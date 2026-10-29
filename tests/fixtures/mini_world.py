"""
Hand-made test fixtures (NOT used by the app - the app only uses real OSM data).

A straight two-way "highway" running due SOUTH (like NH66 towards Goa).
Driving south, the LEFT side of the road is EAST.

    node k  = (LAT0 - k * STEP, LON0)        roughly 1 km apart
"""
import json

from db.database import Database
from db.seed import seed_defaults
from engine.opening_hours import parse_opening_hours
from engine.routing import RoadGraph, build_route

LAT0, LON0 = 18.9, 73.5
STEP = 0.009           # ~1 km between highway nodes
M_PER_DEG_LON = 105_000  # at ~18 deg N
SPEED_KMPH = 60


def _edge(adj, coords, u, v, speed=SPEED_KMPH, lit=None):
    from engine.geometry import haversine_m
    d = haversine_m(*coords[u], *coords[v])
    adj.setdefault(u, []).append((v, d, d / (speed / 3.6), {"highway": "trunk", "lit": lit}))


def build_highway(n_nodes, lit=None):
    """Two-way straight highway; returns (adj, coords)."""
    adj, coords = {}, {}
    for k in range(n_nodes):
        coords[k] = (LAT0 - k * STEP, LON0)
    for k in range(n_nodes - 1):
        _edge(adj, coords, k, k + 1, lit=lit)
        _edge(adj, coords, k + 1, k, lit=lit)
    return adj, coords


def east(k, metres, south_frac=0.5):
    """Point `metres` east (= LEFT when driving south) of the highway, between node k and k+1."""
    return (LAT0 - (k + south_frac) * STEP, LON0 + metres / M_PER_DEG_LON)


def west(k, metres, south_frac=0.5):
    """Point `metres` west (= RIGHT when driving south)."""
    return (LAT0 - (k + south_frac) * STEP, LON0 - metres / M_PER_DEG_LON)


def make_restaurant(name, latlon, opening_hours="Mo-Su 06:00-23:30", amenity="restaurant",
                    veg_type="both", cuisines=None, lit=True, lit_verified=True,
                    parking=True, parking_verified=True, cost="medium", osm_id=None):
    """Restaurant row in the same shape db/seed.py produces."""
    h = parse_opening_hours(opening_hours)
    return {
        "osm_id": osm_id or f"test/{name}", "name": name, "amenity_type": amenity,
        "lat": latlon[0], "lon": latlon[1], "opens": h["opens"], "closes": h["closes"],
        "is_24x7": h["is_24x7"], "hours_verified": h["hours_verified"],
        "opening_hours_raw": opening_hours,
        "hours_json": json.dumps({str(k): v for k, v in h["schedule"].items()}),
        "veg_type": veg_type, "cuisines": cuisines or [amenity], "avg_cost_band": cost,
        "has_parking": parking, "parking_verified": parking_verified,
        "is_well_lit": lit if lit_verified else None, "lighting_verified": lit_verified,
        "source": "test fixture",
    }


def make_db(tmp_path, restaurants):
    """Fresh SQLite DB with default config/rules plus the given restaurants."""
    db = Database(force_sqlite=True, sqlite_path=str(tmp_path / "test.sqlite"))
    db.create_schema()
    seed_defaults(db)
    for r in restaurants:
        db.insert_restaurant(r)
    return db


def full_route(adj, coords, n_nodes):
    """Route over the whole highway, node 0 -> node n-1."""
    return build_route(adj, coords, list(range(n_nodes)))


def road_graph(adj, coords):
    return RoadGraph(adj, coords)
