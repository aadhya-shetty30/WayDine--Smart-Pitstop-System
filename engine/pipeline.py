"""
Glue code: runs every candidate restaurant through the filters in order and
records exactly why each one was accepted or rejected.

    Task A  corridor   ST_DWithin -> projection/chainage -> "Behind you"
    Task B  side       cross product -> "Opposite side, needs U-turn"
    Task A  detour     our Dijkstra -> "Detour too long: X km"
    Task C  time       arrival time, preferences, opening hours, meal type
    Night   safety     rule engine from safety_rules
    Ranking            weighted score, top N with heapq
"""
import re

from engine import corridor, night_mode, ranking, side_filter, time_rules
from engine.opening_hours import hhmm_to_min, min_to_hhmm
from engine.menu_rules import template_keys


def public_restaurant(r):
    """Restaurant fields safe to send to the frontend, plus which ones are guesses."""
    unverified = ["cost_band", "menu"]  # always rule-based
    if not r["hours_verified"]:
        unverified.append("hours")
    if r["veg_type"] == "unknown":
        unverified.append("veg")
    if not r["parking_verified"]:
        unverified.append("parking")
    if not r["lighting_verified"]:
        unverified.append("lighting")
    keys = ["id", "osm_id", "name", "amenity_type", "lat", "lon", "opens", "closes",
            "is_24x7", "hours_verified", "opening_hours_raw", "veg_type", "cuisines",
            "avg_cost_band", "has_parking", "parking_verified", "is_well_lit",
            "lighting_verified", "source"]
    out = {k: r.get(k) for k in keys}
    out["unverified"] = unverified
    return out


class Context:
    """Everything loaded from the DB once per request (thresholds, rules, templates)."""

    def __init__(self, db):
        self.cfg = db.config()
        self.meal_rules = db.meal_rules()
        self.safety_rules = db.safety_rules()
        self.templates = db.menu_templates()


def evaluate(graph, db, route, departure="06:00", weekday=0, prefs=None,
             force_night=False, current_km=None, ctx=None):
    """
    Evaluate every restaurant near the route.

    Returns dict with:
      accepted  - ranked list (all accepted, best first)
      top       - top N recommendations
      rejected  - list with 'reason' and 'stage'
      reachable - places that pass A, B and preferences (input for the stop planner)
    """
    prefs = prefs or {}
    ctx = ctx or Context(db)
    cfg = ctx.cfg
    dep_min = hhmm_to_min(departure)
    geom = corridor.RouteGeometry(route)
    vehicle_m = corridor.current_chainage(geom, current_km=current_km)
    vehicle_s = corridor.time_at_chainage(route, vehicle_m)  # drive time already done

    accepted, rejected, reachable = [], [], []
    for rest in corridor.corridor_candidates(db, route, cfg):
        proj = geom.project(rest["lat"], rest["lon"])
        item = {
            "restaurant": public_restaurant(rest),
            "chainage_km": round(proj["chainage_m"] / 1000, 2),
            "offset_m": round(proj["offset_m"]),
            "warnings": [],
        }

        def reject(stage, reason):
            item.update(stage=stage, reason=reason)
            rejected.append(item)

        # --- Task A: ahead of the vehicle?
        reason = corridor.check_ahead(proj, vehicle_m)
        if reason:
            reject("A", reason)
            continue

        # --- Task B: side of road (needs the detour path for right-side places)
        side, heading = side_filter.side_of_road(geom, proj, rest["lat"], rest["lon"])
        det = corridor.compute_detour(graph, route, proj, rest, cfg)
        item.update(side=side, heading=heading)
        reason = side_filter.check_side(side, det, cfg)
        if reason:
            reject("B", reason)
            continue

        # --- Task A: detour limits
        reason = corridor.check_detour(det, cfg)
        if reason:
            reject("A", reason)
            continue
        item["detour_km"] = round(det["detour_m"] / 1000, 2)
        item["detour_min"] = round(det["detour_s"] / 60, 1)

        # --- Task C: preferences (veg / budget)
        reason, warnings = time_rules.check_preferences(rest, prefs)
        item["warnings"] += warnings
        if reason:
            reject("C", reason)
            continue

        arrival = time_rules.arrival_minute(dep_min, proj["cum_time_s"], det["detour_s"])
        wd, minute = time_rules.split_day(arrival, weekday)
        item["arrival_min"] = round(arrival, 1)
        item["arrival"] = min_to_hhmm(minute)
        item["eta_min"] = round(arrival - dep_min - vehicle_s / 60, 1)  # minutes from "now"
        item["cuisine_match"] = time_rules.cuisine_match(rest, prefs)
        item["template_keys"] = template_keys(rest["cuisines"], rest["amenity_type"])
        item["_row"] = rest  # full row (with schedule) for the planner / gap finder
        reachable.append(item)

        # --- Task C: opening hours + meal window at arrival
        meal, reason = time_rules.check_hours_and_meal(rest, wd, minute, ctx.meal_rules)
        if reason:
            reject("C", reason)
            continue

        # --- Night safety mode
        night = force_night or night_mode.is_night(minute, cfg)
        item["night"] = night
        item["bonuses"] = []
        if night:
            reason, w, bonuses = night_mode.apply_safety_rules(
                rest, ctx.safety_rules, {"open_at_arrival": True})
            item["warnings"] += w
            item["bonuses"] = bonuses
            if reason:
                reject("Night", reason)
                continue

        item["meal"] = meal
        item["typical_items"] = time_rules.typical_items(rest, meal, prefs, ctx.templates,
                                                        item["template_keys"])
        accepted.append(item)

    top = ranking.rank(accepted, prefs, cfg)
    accepted.sort(key=lambda it: -it["score"])
    return {"accepted": accepted, "top": top, "rejected": rejected,
            "reachable": reachable, "vehicle_km": round(vehicle_m / 1000, 2)}


def reason_group(reason):
    """'Closed at 23:40' -> 'Closed at', 'Detour too long: 3.4 km' -> 'Detour too long'."""
    return re.split(r"[:(]|\s\d", reason)[0].strip()


def strip_private(items):
    """Remove internal keys (full DB row) before sending JSON."""
    return [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]
