"""Phase 3 tests: Task A (corridor, behind, detour) and Task B (side of road)."""
import pytest

from engine.pipeline import evaluate
from tests.fixtures.mini_world import (LAT0, LON0, M_PER_DEG_LON, STEP, _edge, build_highway,
                                       east, full_route, make_db, make_restaurant,
                                       road_graph, west)

N = 40


@pytest.fixture
def world(tmp_path):
    adj, coords = build_highway(N)

    # Side lane 170 m east of the highway from km 15 to km 25 that only joins
    # the highway at node 30 -> anything on it needs a long detour.
    for k in range(15, 26):
        coords[1000 + k] = (LAT0 - k * STEP, LON0 + 170 / M_PER_DEG_LON)
    for k in range(15, 25):
        _edge(adj, coords, 1000 + k, 1000 + k + 1)
        _edge(adj, coords, 1000 + k + 1, 1000 + k)
    coords[1030] = (LAT0 - 30 * STEP, LON0 + 170 / M_PER_DEG_LON)
    for a, b in [(1025, 1030), (1030, 1025), (30, 1030), (1030, 30)]:
        _edge(adj, coords, a, b)

    # Underpass on the RIGHT (west) side: one-way 20 -> W -> 22
    coords[2000] = (LAT0 - 21 * STEP, LON0 - 120 / M_PER_DEG_LON)
    _edge(adj, coords, 20, 2000)
    _edge(adj, coords, 2000, 22)

    rests = [
        make_restaurant("Left Dhaba", east(10, 80)),
        make_restaurant("Right Dhaba", west(12, 80)),
        make_restaurant("Behind Dhaba", east(3, 80)),
        make_restaurant("Far Lane Dhaba", (LAT0 - 16 * STEP, LON0 + 185 / M_PER_DEG_LON)),
        make_restaurant("Underpass Cafe", (LAT0 - 21 * STEP, LON0 - 130 / M_PER_DEG_LON)),
        make_restaurant("Too Far Away", east(5, 900)),  # outside the 200 m buffer
    ]
    db = make_db(tmp_path, rests)
    return road_graph(adj, coords), db, full_route(adj, coords, N)


def _by_name(result):
    acc = {it["restaurant"]["name"]: it for it in result["accepted"]}
    rej = {it["restaurant"]["name"]: it for it in result["rejected"]}
    return acc, rej


def test_left_side_accepted(world):
    graph, db, route = world
    acc, _ = _by_name(evaluate(graph, db, route, departure="12:30", current_km=5))
    assert "Left Dhaba" in acc
    assert acc["Left Dhaba"]["side"] == "left"
    assert acc["Left Dhaba"]["detour_km"] < 0.5


def test_right_side_rejected(world):
    graph, db, route = world
    _, rej = _by_name(evaluate(graph, db, route, departure="12:30", current_km=5))
    assert rej["Right Dhaba"]["reason"] == "Opposite side, needs U-turn"


def test_behind_rejected(world):
    graph, db, route = world
    _, rej = _by_name(evaluate(graph, db, route, departure="12:30", current_km=5))
    assert rej["Behind Dhaba"]["reason"].startswith("Behind you")


def test_high_detour_rejected(world):
    graph, db, route = world
    _, rej = _by_name(evaluate(graph, db, route, departure="12:30", current_km=5))
    assert rej["Far Lane Dhaba"]["reason"].startswith("Detour too long")


def test_right_side_with_underpass_accepted(world):
    graph, db, route = world
    acc, _ = _by_name(evaluate(graph, db, route, departure="12:30", current_km=5))
    assert acc["Underpass Cafe"]["side"] == "right"


def test_outside_buffer_not_a_candidate(world):
    graph, db, route = world
    acc, rej = _by_name(evaluate(graph, db, route, departure="12:30"))
    assert "Too Far Away" not in acc and "Too Far Away" not in rej
