"""Phase 5 tests: Smart Stop Planner (DP) and Food Gap Alert (sliding window)."""
from engine.food_gap import find_food_gaps
from engine.pipeline import Context, evaluate
from engine.stop_planner import plan_trip
from tests.fixtures.mini_world import (build_highway, east, full_route, make_db,
                                       make_restaurant, road_graph)


def test_0600_departure_covers_all_meals_and_rests(tmp_path):
    # 900 km straight highway at 60 km/h = 15 h of driving
    n = 901
    adj, coords = build_highway(n)
    rests = [make_restaurant(f"Stop {km}", east(km, 60), "24/7", osm_id=f"t/{km}")
             for km in range(5, 900, 10)]
    db = make_db(tmp_path, rests)
    route = full_route(adj, coords, n)
    ctx = Context(db)
    res = evaluate(road_graph(adj, coords), db, route, departure="06:00", ctx=ctx)
    plan = plan_trip(res["reachable"], route, "06:00", 0, ctx)

    assert plan["meals_required"] == ["breakfast", "lunch", "snacks", "dinner"]
    assert plan["meals_missed"] == []
    assert plan["overdue_legs"] == 0
    types = [s["type"] for s in plan["timeline"]]
    assert types[0] == "start" and types[-1] == "end"
    assert "rest" in types
    meals_in_plan = [s["meal"] for s in plan["timeline"] if s["type"] in ("meal", "both")]
    assert meals_in_plan == ["breakfast", "lunch", "snacks", "dinner"]
    # every chosen stop has typical items and stops are in chainage order
    stops = plan["timeline"][1:-1]
    assert all(s["typical_items"] for s in stops)
    assert [s["chainage_km"] for s in stops] == sorted(s["chainage_km"] for s in stops)


def test_planner_rechecks_hours_after_shifting(tmp_path):
    # 300 km at 60 km/h, 06:00 departure. A stop is needed by km 120 (rest rule).
    #   A at km 100 (24x7)            -> arrive ~07:40, must stop here
    #   B at km 200 closes 09:30      -> 09:20 without stops, but after A's stop it is closed
    #   C at km 210 (24x7)            -> must be used instead of B
    n = 301
    adj, coords = build_highway(n)
    rests = [
        make_restaurant("A", east(100, 60), "24/7"),
        make_restaurant("B", east(200, 60), "06:00-09:30"),
        make_restaurant("C", east(210, 60), "24/7"),
    ]
    db = make_db(tmp_path, rests)
    route = full_route(adj, coords, n)
    ctx = Context(db)
    res = evaluate(road_graph(adj, coords), db, route, departure="06:00", ctx=ctx)
    plan = plan_trip(res["reachable"], route, "06:00", 0, ctx)
    names = [s["name"] for s in plan["timeline"][1:-1]]
    assert names == ["A", "C"]
    assert plan["timeline"][1]["meal"] == "breakfast"
    assert plan["overdue_legs"] == 0


def test_60km_empty_stretch_raises_food_gap(tmp_path):
    n = 121
    adj, coords = build_highway(n)
    kms = [5, 15, 25, 30, 95, 105, 115]  # nothing between km 30 and km 95
    rests = [make_restaurant(f"Dhaba {km}", east(km, 60), "24/7", osm_id=f"t/{km}") for km in kms]
    db = make_db(tmp_path, rests)
    route = full_route(adj, coords, n)
    ctx = Context(db)
    res = evaluate(road_graph(adj, coords), db, route, departure="21:30", ctx=ctx)
    gaps = find_food_gaps(res["accepted"], route, ctx.cfg)
    assert len(gaps) == 1
    g = gaps[0]
    assert g["last_stop_before_gap"] == "Dhaba 30"
    assert g["next_stop_after_gap"] == "Dhaba 95"
    assert 60 < g["length_km"] < 70
    assert g["message"].startswith("Last open stop for 6")


def test_no_gap_when_places_are_dense(tmp_path):
    n = 61
    adj, coords = build_highway(n)
    rests = [make_restaurant(f"D{km}", east(km, 60), "24/7", osm_id=f"t/{km}") for km in range(2, 60, 8)]
    db = make_db(tmp_path, rests)
    route = full_route(adj, coords, n)
    ctx = Context(db)
    res = evaluate(road_graph(adj, coords), db, route, departure="12:00", ctx=ctx)
    assert find_food_gaps(res["accepted"], route, ctx.cfg) == []
