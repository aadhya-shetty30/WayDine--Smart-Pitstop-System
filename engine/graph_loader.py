"""
Phase 1 - Road graph loader.

OSMnx is used ONLY to download OpenStreetMap roads. All routing is done by our
own Dijkstra / A* in engine/routing.py on the adjacency list built here.

The download area is a narrow polygon around a hand-drawn NH66 polyline
(Panvel -> Panaji) so the graph stays small: only motorway, trunk, primary
and their _link roads.
"""
import os
from collections import defaultdict

from shapely.geometry import LineString

from engine.geometry import geom_from_utm, geom_to_utm

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
GRAPH_PATH = os.path.join(DATA_DIR, "nh66.graphml")

# Approximate NH66 path, town by town (lat, lon). Only used to shape the
# download polygon - the actual road geometry comes from OSM.
NH66_WAYPOINTS = [
    (18.990, 73.117),  # Panvel
    (18.860, 73.110),  # Karnala
    (18.737, 73.096),  # Pen
    (18.540, 73.130),  # Nagothane
    (18.430, 73.240),  # Kolad
    (18.330, 73.270),  # Indapur
    (18.235, 73.285),  # Mangaon
    (18.080, 73.420),  # Mahad
    (17.985, 73.465),  # Poladpur
    (17.860, 73.420),  # Kashedi ghat
    (17.730, 73.395),  # Khed (Bharane Naka)
    (17.530, 73.515),  # Chiplun
    (17.360, 73.560),  # Aravali
    (17.190, 73.555),  # Sangameshwar
    (17.030, 73.440),  # Hathkhamba
    (16.860, 73.545),  # Lanja
    (16.655, 73.520),  # Rajapur
    (16.550, 73.610),  # Kharepatan
    (16.420, 73.680),  # Nandgaon
    (16.270, 73.710),  # Kankavli
    (16.130, 73.690),  # Kasal
    (16.005, 73.690),  # Kudal
    (15.900, 73.770),  # Zarap / Sawantwadi bypass
    (15.790, 73.830),  # Patradevi (Goa border)
    (15.700, 73.800),  # Pernem
    (15.595, 73.810),  # Mapusa
    (15.530, 73.820),  # Porvorim
    (15.490, 73.828),  # Panaji
]

# Width of the download corridor on each side of the polyline (metres)
DOWNLOAD_BUFFER_M = 4000

ROAD_FILTER = (
    '["highway"~"motorway|trunk|primary|motorway_link|trunk_link|primary_link"]'
)

# Default speeds (km/h) when OSM has no maxspeed tag
DEFAULT_SPEEDS = {
    "motorway": 80, "motorway_link": 80,
    "trunk": 60, "trunk_link": 60,
    "primary": 50, "primary_link": 50,
}


def corridor_polygon(points_latlon, buffer_m):
    """Buffer a lat/lon polyline by buffer_m metres (done in UTM) -> WGS84 polygon."""
    line = LineString([(lon, lat) for lat, lon in points_latlon])
    line_utm = geom_to_utm(line)
    poly_utm = line_utm.buffer(buffer_m)
    return geom_from_utm(poly_utm)


# Overpass servers to rotate through; override with a comma-separated list in
# WAYDINE_OVERPASS_URLS, e.g. "https://overpass-api.de/api,https://overpass.kumi.systems/api"
OVERPASS_ENDPOINTS = os.environ.get("WAYDINE_OVERPASS_URLS", "https://overpass-api.de/api").split(",")


def configure_osmnx():
    """Shared OSMnx settings: keep the 'lit' tag, cache inside data/."""
    import osmnx as ox

    # keep the OSM "lit" tag on edges - used later for night safety
    if "lit" not in ox.settings.useful_tags_way:
        ox.settings.useful_tags_way = list(ox.settings.useful_tags_way) + ["lit"]
    ox.settings.requests_timeout = 300
    ox.settings.cache_folder = os.path.join(DATA_DIR, "osm_cache")
    return ox


def overpass_retry(download_fn, attempts=10):
    """
    Public Overpass servers are often busy. Retry with exponential backoff
    and rotate between mirror endpoints.
    """
    import time

    ox = configure_osmnx()
    wait = 10
    for i in range(attempts):
        ox.settings.overpass_url = OVERPASS_ENDPOINTS[i % len(OVERPASS_ENDPOINTS)]
        try:
            return download_fn()
        except Exception as e:  # network / server busy
            if i == attempts - 1:
                raise
            print(f"  Overpass error via {ox.settings.overpass_url}: {str(e)[:80]} - retry in {wait}s")
            time.sleep(wait)
            wait = min(wait * 2, 120)


NH66_RELATION_ID = 5828163          # OSM route relation "NH66"
RELATION_PATH = os.path.join(DATA_DIR, "nh66_relation.json")
RELATION_BUFFER_M = 1500            # narrow band around the real NH66 geometry
CORRIDOR_BBOX = (15.40, 19.05, 72.90, 74.00)   # lat_min, lat_max, lon_min, lon_max


def nh66_relation_lines():
    """
    Real NH66 geometry (list of [(lat, lon), ...] ways) from the OSM route
    relation, fetched once from the main OSM API and cached in data/.
    The hand-drawn NH66_WAYPOINTS miss the road by up to 5 km in places
    (Pen, Mahad), so the download area is built from this instead.
    """
    import json
    import xml.etree.ElementTree as ET

    import requests

    if os.path.exists(RELATION_PATH):
        with open(RELATION_PATH, encoding="utf-8") as f:
            return json.load(f)
    print("Fetching NH66 route relation from the OSM API ...")
    url = f"https://api.openstreetmap.org/api/0.6/relation/{NH66_RELATION_ID}/full"
    resp = requests.get(url, timeout=300, headers={"User-Agent": "WayDine-hackathon"})
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    nodes = {n.get("id"): (float(n.get("lat")), float(n.get("lon"))) for n in root.iter("node")}
    lat0, lat1, lon0, lon1 = CORRIDOR_BBOX
    lines = []
    for way in root.iter("way"):
        pts = [nodes[nd.get("ref")] for nd in way.iter("nd") if nd.get("ref") in nodes]
        # keep only the Panvel - Panaji part of this long national highway
        if len(pts) >= 2 and any(lat0 <= a <= lat1 and lon0 <= b <= lon1 for a, b in pts):
            lines.append(pts)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RELATION_PATH, "w", encoding="utf-8") as f:
        json.dump(lines, f)
    return lines


def relation_polygon(buffer_m):
    """Union of all NH66 ways, buffered by buffer_m metres -> WGS84 polygon."""
    from shapely.geometry import MultiLineString

    lines = MultiLineString([[(lon, lat) for lat, lon in way] for way in nh66_relation_lines()])
    return geom_from_utm(geom_to_utm(lines).buffer(buffer_m))


GAP_DIR = os.path.join(DATA_DIR, "osm_gap_tiles")
MAIN_ROADS = {"motorway", "trunk", "primary", "motorway_link", "trunk_link", "primary_link"}


def uncovered_boxes(G, sample_m=500, covered_m=300, margin_deg=0.01):
    """
    Bounding boxes (lat0, lon0, lat1, lon1) along the real NH66 geometry
    where graph G has no road node within covered_m metres.
    Uses a GridIndex of G's nodes; consecutive uncovered samples are
    grouped into one box.
    """
    from engine.geometry import GridIndex, haversine_m

    index = GridIndex(cell_deg=0.005)
    for n, d in G.nodes(data=True):
        index.add(n, d["y"], d["x"])

    boxes = []
    for way in nh66_relation_lines():
        current = []
        for (a1, o1), (a2, o2) in zip(way, way[1:]):
            steps = max(1, int(haversine_m(a1, o1, a2, o2) // sample_m))
            for s in range(steps + 1):
                lat, lon = a1 + (a2 - a1) * s / steps, o1 + (o2 - o1) * s / steps
                _, d = index.nearest(lat, lon, max_rings=2)
                if d > covered_m:
                    current.append((lat, lon))
                elif current:
                    boxes.append(current)
                    current = []
        if current:
            boxes.append(current)
    out = []
    for pts in boxes:
        lats, lons = [p[0] for p in pts], [p[1] for p in pts]
        out.append((min(lats) - margin_deg, min(lons) - margin_deg,
                    max(lats) + margin_deg, max(lons) + margin_deg))
    return _split_boxes(out)


def _split_boxes(boxes, max_side=0.12):
    """
    Merge overlapping boxes (both carriageways give near-identical boxes),
    then cut big ones into tiles, since the OSM API only serves small areas.
    """
    from shapely.geometry import box
    from shapely.ops import unary_union

    merged = unary_union([box(lon0, lat0, lon1, lat1) for lat0, lon0, lat1, lon1 in boxes])
    parts = getattr(merged, "geoms", [merged])
    tiles = set()
    for part in parts:
        lon0, lat0, lon1, lat1 = part.bounds
        lat = lat0
        while lat < lat1:
            lon = lon0
            while lon < lon1:
                tiles.add((round(lat, 4), round(lon, 4),
                           round(min(lat + max_side, lat1), 4), round(min(lon + max_side, lon1), 4)))
                lon += max_side
            lat += max_side
    return sorted(tiles)


def fill_gaps_from_osm_api(G):
    """
    Fill holes in G using the main OSM API (/api/0.6/map), which returns
    OSM XML for a small box. OSMnx only *reads* that XML (graph_from_xml);
    we then keep main roads only. Tiles are cached in data/osm_gap_tiles/.
    """
    import networkx as nx

    tiles = uncovered_boxes(G)
    print(f"Filling {len(tiles)} uncovered NH66 tiles from the OSM API ...")
    for tile in tiles:
        T = _fetch_tile_graph(*tile)
        if T is not None:
            G = nx.compose(G, T)
    return bridge_components(G)


def _fetch_tile_graph(lat0, lon0, lat1, lon1):
    """One OSM API box -> OSMnx graph of main roads only (cached on disk)."""
    import osmnx as ox
    import requests

    os.makedirs(GAP_DIR, exist_ok=True)
    path = os.path.join(GAP_DIR, f"{lat0}_{lon0}_{lat1}_{lon1}.osm")
    if not os.path.exists(path):
        resp = requests.get("https://api.openstreetmap.org/api/0.6/map",
                            params={"bbox": f"{lon0},{lat0},{lon1},{lat1}"},
                            timeout=180, headers={"User-Agent": "WayDine-hackathon"})
        resp.raise_for_status()
        with open(path, "wb") as f:
            f.write(resp.content)
    try:
        T = ox.graph_from_xml(path, simplify=False, retain_all=True)
    except Exception as e:  # tile with no roads at all
        print(f"  skip tile {path}: {str(e)[:60]}")
        return None
    main = [(u, v, k) for u, v, k, d in T.edges(keys=True, data=True)
            if _first(d.get("highway")) in MAIN_ROADS]
    return T.edge_subgraph(main).copy()


def bridge_components(G, min_size=1000, max_rounds=10, margin_deg=0.01):
    """
    While the graph is still split into big pieces, find the closest pair
    of nodes between the two largest pieces and fetch the OSM API box
    around that pair (usually a junction just outside the download area).
    """
    import networkx as nx

    from engine.geometry import GridIndex

    for _ in range(max_rounds):
        comps = [c for c in sorted(nx.weakly_connected_components(G), key=len, reverse=True)
                 if len(c) >= min_size]
        if len(comps) < 2:
            break
        index = GridIndex(cell_deg=0.01)
        for n in comps[0]:
            index.add(n, G.nodes[n]["y"], G.nodes[n]["x"])
        best = (float("inf"), None, None)
        for n in comps[1]:
            m, d = index.nearest(G.nodes[n]["y"], G.nodes[n]["x"], max_rings=5)
            if d < best[0]:
                best = (d, n, m)
        d, a, b = best
        if a is None:
            break
        ya, xa, yb, xb = G.nodes[a]["y"], G.nodes[a]["x"], G.nodes[b]["y"], G.nodes[b]["x"]
        print(f"  bridging a {d:.0f} m gap near {ya:.4f},{xa:.4f}")
        T = _fetch_tile_graph(round(min(ya, yb) - margin_deg, 4), round(min(xa, xb) - margin_deg, 4),
                              round(max(ya, yb) + margin_deg, 4), round(max(xa, xb) + margin_deg, 4))
        if T is None:
            break
        before = len(G)
        G = nx.compose(G, T)
        if len(G) == before:
            break  # nothing new - give up rather than loop
    return G


def download_graph():
    """
    Download the NH66 corridor from OSM and save it as GraphML.

    1. Overpass: wide band around the hand-drawn waypoints (NH66 + main roads nearby)
    2. Where that band misses the real NH66 geometry (OSM route relation),
       fetch the missing boxes from the main OSM API and merge them in
       (shared OSM node ids join the pieces).
    Only the largest strongly connected piece is kept, so every node can
    reach every other node legally.
    """
    ox = configure_osmnx()
    print("Downloading NH66 road network from OpenStreetMap ...")
    # simplify=False keeps every OSM node, so a restaurant can be snapped to a
    # nearby road node (needed for the detour calculation)
    wide = corridor_polygon(NH66_WAYPOINTS, DOWNLOAD_BUFFER_M)
    G = overpass_retry(lambda: ox.graph_from_polygon(
        wide, custom_filter=ROAD_FILTER, simplify=False, retain_all=True))
    G = fill_gaps_from_osm_api(G)
    G = ox.truncate.largest_component(G, strongly=True)
    G = ox.add_edge_speeds(G, hwy_speeds=DEFAULT_SPEEDS)
    G = ox.add_edge_travel_times(G)
    os.makedirs(DATA_DIR, exist_ok=True)
    ox.save_graphml(G, GRAPH_PATH)
    print(f"Saved {len(G.nodes)} nodes / {len(G.edges)} edges to {GRAPH_PATH}")
    return G


def load_osm_graph():
    """Load the GraphML from disk, downloading it first if missing."""
    import osmnx as ox

    if os.path.exists(GRAPH_PATH):
        return ox.load_graphml(GRAPH_PATH)
    return download_graph()


def _first(value):
    """OSM tags can be lists after merging; take the first value."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def to_adjacency(G):
    """
    Convert the OSMnx MultiDiGraph into our own plain data structures:

    adj    = {node: [(neighbour, length_m, travel_time_s, attrs), ...]}
    coords = {node: (lat, lon)}

    OSMnx already models one-way roads as a single directed edge and two-way
    roads as two edges, so following adj respects one-way rules.
    If there are parallel edges u->v we keep only the shortest.
    """
    coords = {n: (float(d["y"]), float(d["x"])) for n, d in G.nodes(data=True)}
    best = {}
    for u, v, d in G.edges(data=True):
        length = float(d.get("length", 0.0))
        if (u, v) in best and best[(u, v)][0] <= length:
            continue
        attrs = {
            "highway": _first(d.get("highway")),
            "name": _first(d.get("name")),
            "ref": _first(d.get("ref")),
            "lit": _first(d.get("lit")),  # "yes" / "no" / None (unknown)
            "has_maxspeed": d.get("maxspeed") is not None,  # posted limit in OSM
        }
        best[(u, v)] = (length, float(d.get("travel_time", 0.0)), attrs)

    adj = defaultdict(list)
    for (u, v), (length, tt, attrs) in best.items():
        adj[u].append((v, length, tt, attrs))
    return dict(adj), coords


def load_graph():
    """Convenience: returns (adj, coords) ready for engine.routing."""
    return to_adjacency(load_osm_graph())


if __name__ == "__main__":
    adj, coords = load_graph()
    print(f"Adjacency list: {len(coords)} nodes, {sum(len(v) for v in adj.values())} directed edges")
