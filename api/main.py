"""
WayDine API (FastAPI).

    POST /route          route + chainage summary
    POST /recommend      ranked accepted places + rejected places with reasons
    POST /plan-trip      Smart Stop Planner timeline
    POST /food-gaps      stretches with no open food stop
    POST /trip           all of the above in one call (used by the frontend)
    GET  /restaurants/{id}
    GET  /data-quality
    GET  /presets

The road graph is loaded once at startup; the frontend is served from /.
Run from the waydine/ folder:  uvicorn api.main:app --reload
"""
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls
from functools import lru_cache
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from db.database import Database
from engine.congestion import CongestionModel
from engine.food_gap import find_food_gaps
from engine.graph_loader import load_graph
from engine.menu_rules import template_keys
from engine.night_mode import is_night
from engine.opening_hours import hhmm_to_min
from engine.pipeline import Context, evaluate, public_restaurant, reason_group, strip_private
from engine.routing import RoadGraph
from engine.stop_planner import plan_trip
from engine.time_rules import typical_items

ROOT = os.path.dirname(os.path.dirname(__file__))

# Preset points on the NH66 corridor (lat, lon)
PRESETS = {
    "mumbai_panvel": {"label": "Mumbai (Panvel)", "lat": 18.9894, "lon": 73.1175},
    "pen": {"label": "Pen", "lat": 18.7370, "lon": 73.0960},
    "mahad": {"label": "Mahad", "lat": 18.0830, "lon": 73.4180},
    "chiplun": {"label": "Chiplun", "lat": 17.5330, "lon": 73.5150},
    "kankavli": {"label": "Kankavli", "lat": 16.2700, "lon": 73.7100},
    "mapusa": {"label": "Goa (Mapusa)", "lat": 15.5930, "lon": 73.8090},
    "goa_panaji": {"label": "Goa (Panaji)", "lat": 15.4909, "lon": 73.8278},
}

STATE = {}


@asynccontextmanager
async def lifespan(app):
    """Load the road graph and open the database once at startup."""
    STATE["graph"] = RoadGraph(*load_graph())
    STATE["db"] = Database()
    print(f"WayDine ready: {len(STATE['graph'].coords)} road nodes, DB backend = {STATE['db'].backend}")
    yield


app = FastAPI(title="WayDine", description="Route-based highway restaurant recommendations",
              lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------------ models
class Point(BaseModel):
    lat: float
    lon: float


class RouteRequest(BaseModel):
    source: Point = Field(default_factory=lambda: Point(**{k: PRESETS["mumbai_panvel"][k] for k in ("lat", "lon")}))
    destination: Point = Field(default_factory=lambda: Point(**{k: PRESETS["goa_panaji"][k] for k in ("lat", "lon")}))
    departure_time: str = "06:00"           # HH:MM
    date: Optional[str] = None              # YYYY-MM-DD, for weekday-specific hours
    current_km: Optional[float] = None      # vehicle position along the route


class Preferences(BaseModel):
    veg_pref: str = "any"                   # veg / nonveg / any
    max_budget_band: str = "high"           # low / medium / high
    preferred_cuisines: List[str] = []


class RecommendRequest(RouteRequest):
    preferences: Preferences = Preferences()
    night_mode: bool = False


# ----------------------------------------------------------------- helpers
def congestion_model():
    """
    Congestion model from the DB tables, cached until the rules/zones change
    (it remembers which zone each road node is in, so reuse saves time).
    """
    db = STATE["db"]
    if not db.config().get("congestion_enabled", 0):
        return None
    key = (tuple(tuple(sorted(r.items())) for r in db.congestion_rules()),
           tuple(tuple(sorted(z.items())) for z in db.congestion_zones()))
    if STATE.get("congestion_key") != key:
        STATE["congestion"] = CongestionModel(db.congestion_zones(), db.congestion_rules(),
                                              STATE["graph"].coords)
        STATE["congestion_key"] = key
        _route.cache_clear()
    return STATE["congestion"]


@lru_cache(maxsize=32)
def _route(src, dst, depart_min, trip_date, use_congestion):
    model = congestion_model() if use_congestion else None
    route = STATE["graph"].route(src, dst, depart_min=depart_min,
                                 start_date=date_cls.fromisoformat(trip_date), model=model)
    if route is None:
        raise HTTPException(404, "No road route found between these points on the NH66 graph")
    return route


def check_snap(point, label):
    """Source/destination must lie near the downloaded NH66 corridor."""
    limit_km = STATE["db"].config()["max_snap_km"]
    _, dist_m = STATE["graph"].locator.nearest_node(point.lat, point.lon)
    if dist_m > limit_km * 1000:
        far = "more than 50" if dist_m == float("inf") else f"{dist_m / 1000:.1f}"
        raise HTTPException(422, f"{label} is {far} km from the NH66 corridor. WayDine covers "
                                 f"Mumbai (Panvel) to Goa (Panaji) only; pick a point within {limit_km:g} km of NH66.")


def get_route(req: RouteRequest):
    validate_time(req.departure_time)
    check_snap(req.source, "Source")
    check_snap(req.destination, "Destination")
    trip_date = req.date or date_cls.today().isoformat()
    use_congestion = congestion_model() is not None
    return _route((round(req.source.lat, 5), round(req.source.lon, 5)),
                  (round(req.destination.lat, 5), round(req.destination.lon, 5)),
                  hhmm_to_min(req.departure_time), trip_date, use_congestion)


def weekday_of(req: RouteRequest):
    d = date_cls.fromisoformat(req.date) if req.date else date_cls.today()
    return d.weekday()


def validate_time(text):
    try:
        m = hhmm_to_min(text)
        assert 0 <= m < 1440
    except Exception:
        raise HTTPException(422, f"departure_time must be HH:MM, got {text!r}")


def route_summary(route, max_points=2500):
    """Route for the map, thinned to at most max_points coordinates."""
    step = max(1, len(route.coords) // max_points)
    coords = route.coords[::step] + [route.coords[-1]]
    traffic = sorted(((name, round(sec / 60, 1)) for name, sec in route.traffic.items()),
                     key=lambda x: -abs(x[1]))
    return {"distance_km": round(route.total_m / 1000, 1),
            "duration_min": round(route.total_s / 60), "points": len(route.coords),
            "free_flow_min": round(route.free_flow_s / 60),
            "traffic_delay_min": round((route.total_s - route.free_flow_s) / 60),
            "traffic": [{"rules": n, "minutes": m} for n, m in traffic if abs(m) >= 0.5],
            "coords": [[round(a, 5), round(b, 5)] for a, b in coords]}


def run_evaluation(req: RecommendRequest):
    validate_time(req.departure_time)
    db, graph = STATE["db"], STATE["graph"]
    route = get_route(req)
    ctx = Context(db)
    result = evaluate(graph, db, route, departure=req.departure_time, weekday=weekday_of(req),
                      prefs=req.preferences.model_dump(), force_night=req.night_mode,
                      current_km=req.current_km, ctx=ctx)
    return route, ctx, result


def night_banner(req, ctx):
    auto = is_night(hhmm_to_min(req.departure_time), ctx.cfg)
    return {"enabled_by_user": req.night_mode, "active_at_departure": auto or req.night_mode,
            "night_start": ctx.cfg["night_start"], "night_end": ctx.cfg["night_end"]}


# ---------------------------------------------------------------- endpoints
@app.get("/presets")
def presets():
    return PRESETS


@app.post("/route")
def route_endpoint(req: RouteRequest):
    validate_time(req.departure_time)
    return route_summary(get_route(req))


@app.get("/congestion-rules")
def congestion_rules():
    """The traffic rules and zones in use (read from the DB)."""
    db = STATE["db"]
    return {"enabled": bool(db.config().get("congestion_enabled", 0)),
            "rules": db.congestion_rules(), "zones": db.congestion_zones()}


@app.post("/recommend")
def recommend(req: RecommendRequest):
    route, ctx, res = run_evaluation(req)
    return {
        "route": {k: v for k, v in route_summary(route).items() if k != "coords"},
        "night_mode": night_banner(req, ctx),
        "top": strip_private(res["top"]),
        "accepted": strip_private(res["accepted"]),
        "rejected": strip_private(res["rejected"]),
        "counts": {"candidates": len(res["accepted"]) + len(res["rejected"]),
                   "accepted": len(res["accepted"]), "rejected": len(res["rejected"])},
    }


@app.post("/plan-trip")
def plan(req: RecommendRequest):
    route, ctx, res = run_evaluation(req)
    return plan_trip(res["reachable"], route, req.departure_time, weekday_of(req), ctx,
                     req.preferences.model_dump(), night=req.night_mode)


@app.post("/food-gaps")
def food_gaps(req: RecommendRequest):
    route, ctx, res = run_evaluation(req)
    return {"food_gap_km": ctx.cfg["food_gap_km"],
            "gaps": find_food_gaps(res["accepted"], route, ctx.cfg, start_km=res["vehicle_km"])}


@app.post("/trip")
def trip(req: RecommendRequest):
    """Everything the frontend needs from one evaluation."""
    route, ctx, res = run_evaluation(req)
    prefs = req.preferences.model_dump()
    rej_counts = {}
    for it in res["rejected"]:
        key = reason_group(it["reason"])
        rej_counts[key] = rej_counts.get(key, 0) + 1
    return {
        "route": route_summary(route),
        "night_mode": night_banner(req, ctx),
        "top": strip_private(res["top"]),
        "accepted": strip_private(res["accepted"]),
        "rejected": strip_private(res["rejected"]),
        "rejection_summary": rej_counts,
        "plan": plan_trip(res["reachable"], route, req.departure_time, weekday_of(req), ctx,
                          prefs, night=req.night_mode),
        "gaps": find_food_gaps(res["accepted"], route, ctx.cfg, start_km=res["vehicle_km"]),
        "food_gap_km": ctx.cfg["food_gap_km"],
    }


@app.get("/restaurants/{rid}")
def restaurant(rid: int):
    db = STATE["db"]
    r = db.restaurant(rid)
    if r is None:
        raise HTTPException(404, "Restaurant not found")
    templates = db.menu_templates()
    keys = template_keys(r["cuisines"], r["amenity_type"])
    menu = {meal: typical_items(r, meal, {}, templates, keys, limit=4)
            for meal in ("breakfast", "lunch", "snacks", "dinner")}
    out = public_restaurant(r)
    out["typical_items"] = menu
    out["typical_items_note"] = "Typical items for this cuisine/type - not the restaurant's actual menu"
    out["osm_url"] = f"https://www.openstreetmap.org/{r['osm_id']}" if r.get("osm_id") else None
    return out


@app.get("/data-quality")
def data_quality():
    q = STATE["db"].data_quality()
    q["backend"] = STATE["db"].backend
    return q


# Frontend (mounted last so the API routes win)
app.mount("/", StaticFiles(directory=os.path.join(ROOT, "frontend"), html=True), name="frontend")
