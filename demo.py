"""
WayDine demo - two scenarios on the same real route (Panvel -> Panaji, NH66).

  1. 06:00 departure: ranked recommendations and the full stop timeline.
  2. 21:30 departure: night mode switches on by itself, unsafe places are
     rejected, and food gaps are reported if the real data has any.

Run from the waydine/ folder:   python demo.py
(needs data/nh66.graphml and a seeded database: python -m db.seed)
"""
import time
from datetime import date

from db.database import Database
from engine.congestion import load_model
from engine.food_gap import find_food_gaps
from engine.graph_loader import load_graph
from engine.pipeline import Context, evaluate, reason_group
from engine.routing import RoadGraph
from engine.stop_planner import plan_trip

PANVEL = (18.9894, 73.1175)
PANAJI = (15.4909, 73.8278)


def line(char="="):
    print(char * 78)


def scenario(graph, db, departure, title):
    line()
    print(f"SCENARIO: {title}  (departure {departure})")
    line()
    ctx = Context(db)
    h, m = map(int, departure.split(":"))
    # time-dependent route: congestion rules depend on when we pass each town / ghat
    route = graph.route(PANVEL, PANAJI, depart_min=h * 60 + m, start_date=date.today(),
                        model=load_model(db, graph.coords))
    delay = (route.total_s - route.free_flow_s) / 60
    top = sorted(route.traffic.items(), key=lambda x: -x[1])[:3]
    print(f"Drive {route.total_s / 3600:.1f} h = free-flow {route.free_flow_s / 3600:.1f} h + "
          f"{delay:.0f} min traffic ({', '.join(f'{k} {v / 60:.0f} min' for k, v in top)})")
    t0 = time.time()
    res = evaluate(graph, db, route, departure=departure, weekday=time.localtime().tm_wday, ctx=ctx)
    plan = plan_trip(res["reachable"], route, departure, time.localtime().tm_wday, ctx)
    gaps = find_food_gaps(res["accepted"], route, ctx.cfg)
    print(f"Candidates within {ctx.cfg['buffer_m']:.0f} m of the route: "
          f"{len(res['accepted']) + len(res['rejected'])}   accepted {len(res['accepted'])}   "
          f"rejected {len(res['rejected'])}   ({time.time() - t0:.1f} s)")

    reasons = {}
    for it in res["rejected"]:
        key = reason_group(it["reason"])
        reasons[key] = reasons.get(key, 0) + 1
    print("Rejections:", ", ".join(f"{k} = {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
    night = [it for it in res["rejected"] if it["stage"] == "Night"]
    if night or any(it.get("night") for it in res["accepted"]):
        print(f"Night safety mode ACTIVE for late arrivals: {len(night)} place(s) rejected as unsafe")

    print("\nTOP RECOMMENDATIONS")
    for i, it in enumerate(res["top"], 1):
        r = it["restaurant"]
        print(f" {i}. {r['name']:<34} km {it['chainage_km']:>6.1f}  score {it['score']:>5}")
        print(f"    {it['explanation']}")
        if it["warnings"]:
            print(f"    warnings: {', '.join(it['warnings'])}")

    print("\nSMART STOP PLAN")
    for s in plan["timeline"]:
        meal = f"{s.get('meal', '')}" if s["type"] in ("meal", "both") else ""
        print(f"  {s['time']:<16} {s['type']:<6} {meal:<10} km {s['chainage_km']:>6.1f}  {s['name']}")
    print(f"  meals required: {plan['meals_required']}  missed: {plan['meals_missed']}  "
          f"overdue legs: {plan['overdue_legs']}  total detour: {plan['total_detour_km']} km")

    print("\nFOOD GAPS (> %g km without an open stop)" % ctx.cfg["food_gap_km"])
    if not gaps:
        print("  none")
    for g in gaps:
        print(f"  km {g['start_km']:>6.1f} -> {g['end_km']:>6.1f}  ({g['length_km']} km)  {g['message']}")
    print()


def main():
    db = Database()
    graph = RoadGraph(*load_graph())
    route = graph.route(PANVEL, PANAJI)
    print(f"Route Panvel -> Panaji: {route.total_m / 1000:.1f} km, {route.total_s / 3600:.1f} h free-flow, "
          f"{len(route.coords)} points  (DB: {db.backend})")
    q = db.data_quality()
    print(f"Data: {q['total_restaurants']} OSM places, hours verified {q['hours_verified']}, "
          f"veg info {q['veg_info_known']}, parking verified {q['parking_verified']}, "
          f"lighting verified {q['lighting_verified']}\n")
    scenario(graph, db, "06:00", "Morning departure")
    scenario(graph, db, "21:30", "Night departure")


if __name__ == "__main__":
    main()
