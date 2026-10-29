"""
Phase 2 - Download REAL restaurants (and parking) along NH66 from OpenStreetMap.

Steps
1. Route Panvel -> Panaji with our own A* (engine.routing).
2. Buffer the route by 1 km (in UTM metres) -> download polygon.
3. Download amenity in [restaurant, fast_food, cafe, food_court] and amenity=parking.
4. Polygons -> centroid points, skip unnamed places, drop duplicates
   (same name within 50 m).
5. Cache to data/nh66_restaurants.geojson and data/nh66_parking.geojson.

Run:  python -m db.fetch_osm_restaurants        (add --refresh to ignore the cache)
      default: one light Overpass query (centre points + tags)
      --osmnx   : OSMnx features_from_polygon (full geometries, heavier)
      --osm-api : main OSM API in small tiles (slow, last resort)
"""
import json
import os
import sys

from shapely.geometry import LineString

from engine.geometry import GridIndex, from_utm, geom_from_utm, geom_to_utm
from engine.graph_loader import DATA_DIR, configure_osmnx, load_graph, overpass_retry
from engine.routing import NodeLocator, find_route

RESTAURANTS_PATH = os.path.join(DATA_DIR, "nh66_restaurants.geojson")
PARKING_PATH = os.path.join(DATA_DIR, "nh66_parking.geojson")

FOOD_AMENITIES = ["restaurant", "fast_food", "cafe", "food_court"]
FETCH_BUFFER_M = 1000   # download corridor, wider than the config buffer_m on purpose
DUPLICATE_M = 50        # same name within this distance = duplicate

PANVEL = (18.9894, 73.1175)
PANAJI = (15.4909, 73.8278)

# OSM tags we keep for the enrichment rules in db/seed.py
KEEP_TAGS = ["name", "amenity", "cuisine", "diet:vegetarian", "opening_hours",
             "parking", "capacity:car", "lit", "addr:full", "addr:city"]


def route_polygon(buffer_m):
    """1 km buffer around the real NH66 route computed by our own A*."""
    adj, coords = load_graph()
    route = find_route(adj, coords, NodeLocator(coords), PANVEL, PANAJI)
    line = LineString([(lon, lat) for lat, lon in route.coords]).simplify(0.0002)
    poly_utm = geom_to_utm(line).buffer(buffer_m)
    return geom_from_utm(poly_utm)


def _centroid_latlon(geom):
    """Centroid computed in UTM metres (correct for polygons), returned as (lat, lon)."""
    c = geom_to_utm(geom).centroid
    return from_utm(c.x, c.y)


def _clean_value(v):
    """Pandas gives NaN for missing tags; turn those into None."""
    if v is None or (isinstance(v, float) and v != v):
        return None
    return str(v)


def _to_features(gdf):
    """GeoDataFrame from OSMnx -> list of plain GeoJSON point features."""
    feats = []
    if gdf is None or len(gdf) == 0:
        return feats
    for idx, row in gdf.iterrows():
        lat, lon = _centroid_latlon(row.geometry)
        osm_type, osm_id = idx if isinstance(idx, tuple) else ("node", idx)
        props = {"osm_id": f"{osm_type}/{osm_id}"}
        for tag in KEEP_TAGS:
            if tag in gdf.columns:
                props[tag] = _clean_value(row[tag])
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]}})
    return feats


def _save(path, feats):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False, indent=1)


def load_cached():
    """Return (restaurants, parking) feature lists from the cache files."""
    with open(RESTAURANTS_PATH, encoding="utf-8") as f:
        rest = json.load(f)["features"]
    with open(PARKING_PATH, encoding="utf-8") as f:
        park = json.load(f)["features"]
    return rest, park


def fetch_from_overpass_light(poly, attempts=60):
    """
    One lightweight Overpass query: every food place and parking in the
    route band, returned as centre points + tags only ("out center tags"),
    so the response is a few hundred KB. Busy servers answer 502 quickly,
    so we simply retry every few seconds.
    """
    import time

    import requests

    ring = poly.simplify(0.002).exterior.coords  # ~200 m tolerance, inside the 1 km band margin
    poly_str = " ".join(f"{lat:.5f} {lon:.5f}" for lon, lat in ring)
    amenities = "|".join(FOOD_AMENITIES + ["parking"])
    query = (f'[out:json][timeout:180];nwr["amenity"~"^({amenities})$"](poly:"{poly_str}");'
             "out center tags;")
    for i in range(attempts):
        try:
            resp = requests.post("https://overpass-api.de/api/interpreter", data={"data": query},
                                 timeout=240, headers={"User-Agent": "WayDine-hackathon"})
            if resp.status_code == 200:
                break
            print(f"  Overpass {resp.status_code}, retry {i + 1}")
        except requests.RequestException as e:
            print(f"  Overpass error {str(e)[:60]}, retry {i + 1}")
        time.sleep(5)
    else:
        raise RuntimeError("Overpass unavailable - try --osm-api")

    food, parking = [], []
    for el in resp.json()["elements"]:
        tags = el.get("tags", {})
        if el["type"] == "node":
            lat, lon = el["lat"], el["lon"]
        elif "center" in el:
            lat, lon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        props = {"osm_id": f"{el['type']}/{el['id']}"}
        props.update({t: tags.get(t) for t in KEEP_TAGS})
        feat = {"type": "Feature", "properties": props,
                "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]}}
        (parking if tags.get("amenity") == "parking" else food).append(feat)
    return food, parking


def fetch_from_osm_api(poly, tile_deg=0.02):
    """
    Fallback when Overpass is unavailable: read the same amenities from the
    main OSM API (/api/0.6/map) in small tiles that touch the route band.
    Tiles are cached in data/osm_poi_tiles/. Returns (food, parking) features.
    """
    import math
    import xml.etree.ElementTree as ET

    import requests
    from shapely.geometry import Point, Polygon, box

    tile_dir = os.path.join(DATA_DIR, "osm_poi_tiles")
    os.makedirs(tile_dir, exist_ok=True)
    minx, miny, maxx, maxy = poly.bounds
    tiles = [(i, j) for i in range(math.floor(miny / tile_deg), math.ceil(maxy / tile_deg))
             for j in range(math.floor(minx / tile_deg), math.ceil(maxx / tile_deg))
             if poly.intersects(box(j * tile_deg, i * tile_deg, (j + 1) * tile_deg, (i + 1) * tile_deg))]
    print(f"Reading {len(tiles)} OSM API tiles along the route ...")
    food, parking, seen = [], [], set()
    for k, (i, j) in enumerate(tiles):
        path = os.path.join(tile_dir, f"{i}_{j}.osm")
        if not os.path.exists(path):
            bbox = f"{j * tile_deg:.4f},{i * tile_deg:.4f},{(j + 1) * tile_deg:.4f},{(i + 1) * tile_deg:.4f}"
            resp = requests.get("https://api.openstreetmap.org/api/0.6/map", params={"bbox": bbox},
                                timeout=180, headers={"User-Agent": "WayDine-hackathon"})
            resp.raise_for_status()
            with open(path, "wb") as f:
                f.write(resp.content)
        if k % 50 == 0:
            print(f"  tile {k}/{len(tiles)}")
        root = ET.parse(path).getroot()
        nodes = {n.get("id"): (float(n.get("lat")), float(n.get("lon"))) for n in root.iter("node")}
        for el in list(root.iter("node")) + list(root.iter("way")):
            tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
            amenity = tags.get("amenity")
            if amenity not in FOOD_AMENITIES and amenity != "parking":
                continue
            osm_id = f"{el.tag}/{el.get('id')}"
            if osm_id in seen:
                continue  # same feature appears in neighbouring tiles
            if el.tag == "node":
                geom = Point(float(el.get("lon")), float(el.get("lat")))
            else:
                pts = [nodes[nd.get("ref")] for nd in el.iter("nd") if nd.get("ref") in nodes]
                if len(pts) < 3:
                    continue
                geom = Polygon([(lon, lat) for lat, lon in pts])
            lat, lon = _centroid_latlon(geom)
            if not poly.contains(Point(lon, lat)):
                continue
            seen.add(osm_id)
            props = {"osm_id": osm_id}
            props.update({t: tags.get(t) for t in KEEP_TAGS})
            feat = {"type": "Feature", "properties": props,
                    "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]}}
            (parking if amenity == "parking" else food).append(feat)
    return food, parking


def fetch(refresh=False):
    if not refresh and os.path.exists(RESTAURANTS_PATH) and os.path.exists(PARKING_PATH):
        rest, park = load_cached()
        print(f"Using cache: {len(rest)} restaurants, {len(park)} parking features")
        return rest, park

    ox = configure_osmnx()
    poly = route_polygon(FETCH_BUFFER_M)
    if "--osm-api" in sys.argv:
        food, parking = fetch_from_osm_api(poly)
    elif "--osmnx" not in sys.argv:
        print("Downloading food places + parking from OpenStreetMap (Overpass, centres only) ...")
        food, parking = fetch_from_overpass_light(poly)
    else:
        print("Downloading food places from OpenStreetMap (Overpass) ...")
        food = _to_features(overpass_retry(
            lambda: ox.features_from_polygon(poly, tags={"amenity": FOOD_AMENITIES})))
        print("Downloading parking from OpenStreetMap (Overpass) ...")
        try:
            parking = _to_features(overpass_retry(
                lambda: ox.features_from_polygon(poly, tags={"amenity": "parking"}), attempts=3))
        except Exception as e:  # OSMnx raises if nothing matches
            print("  no parking found:", e)
            parking = []

    downloaded = len(food)
    named = [f for f in food if f["properties"].get("name")]
    skipped = downloaded - len(named)

    # Duplicate removal: same (case-insensitive) name within 50 m.
    # A grid index per name keeps this fast.
    kept, seen = [], {}
    for f in named:
        lon, lat = f["geometry"]["coordinates"]
        key = f["properties"]["name"].strip().lower()
        grid = seen.setdefault(key, GridIndex(cell_deg=0.001))
        _, d = grid.nearest(lat, lon, max_rings=1)
        if d <= DUPLICATE_M:
            continue
        grid.add(f, lat, lon)
        kept.append(f)
    duplicates = len(named) - len(kept)

    _save(RESTAURANTS_PATH, kept)
    _save(PARKING_PATH, parking)
    print(f"Downloaded: {downloaded}")
    print(f"Skipped (no name): {skipped}")
    print(f"Duplicates removed: {duplicates}")
    print(f"Final restaurants: {len(kept)}")
    print(f"Parking features: {len(parking)}")
    return kept, parking


if __name__ == "__main__":
    fetch(refresh="--refresh" in sys.argv)
