"""
Real-data checks on the downloaded NH66 graph and seeded OSM restaurants.
Skipped automatically until `python -m engine.graph_loader` and `python -m db.seed` have run.
Run with `pytest -s -m real_data` to see the printed scenario results.
"""
import os

import pytest

from engine.graph_loader import GRAPH_PATH

pytestmark = pytest.mark.real_data

PANVEL, PANAJI = (18.9894, 73.1175), (15.4909, 73.8278)


@pytest.fixture(scope="module")
def world():
    if not os.path.exists(GRAPH_PATH):
        pytest.skip("data/nh66.graphml not downloaded yet")
    from db.database import Database
    from engine.graph_loader import load_graph
    from engine.routing import RoadGraph
    db = Database()
    try:
        if not db.all_restaurants():
            pytest.skip("database not seeded")
    except Exception:
        pytest.skip("database not seeded")
    graph = RoadGraph(*load_graph())
    return graph, db, graph.route(PANVEL, PANAJI)


@pytest.mark.parametrize("departure", ["06:00", "21:30"])
def test_real_scenarios(world, departure):
    from engine.food_gap import find_food_gaps
    from engine.pipeline import Context, evaluate
    from engine.stop_planner import plan_trip
    graph, db, route = world
    ctx = Context(db)
    res = evaluate(graph, db, route, departure=departure, ctx=ctx)
    plan = plan_trip(res["reachable"], route, departure, 0, ctx)
    gaps = find_food_gaps(res["accepted"], route, ctx.cfg)
    print(f"\n[{departure}] accepted {len(res['accepted'])}, rejected {len(res['rejected'])}, "
          f"gaps {len(gaps)}, plan stops {len(plan['timeline']) - 2}, missed {plan['meals_missed']}")
    assert res["accepted"] or res["rejected"]
    assert all(it["reason"] for it in res["rejected"])          # every rejection explained
    assert all(it["restaurant"]["source"] == "OpenStreetMap" for it in res["accepted"])
    assert len(res["top"]) <= 5
    if departure == "21:30":
        # arrivals after 22:00 must be evaluated in night mode
        assert any(it.get("night") for it in res["accepted"]) or any(
            it["stage"] == "Night" for it in res["rejected"]) or not res["accepted"]
