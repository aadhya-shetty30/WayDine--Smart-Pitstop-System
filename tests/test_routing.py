"""Phase 1 tests: our own Dijkstra / A* and geometry helpers."""
import os

import pytest

from engine.geometry import compass, cross_product_side, haversine_m
from engine.routing import astar, build_route, dijkstra
from tests.fixtures.mini_world import build_highway


def test_haversine_one_degree_latitude():
    assert abs(haversine_m(18.0, 73.0, 19.0, 73.0) - 111_195) < 200


def test_cross_product_left_is_positive():
    # heading south (0,0) -> (0,-10); a point to the east (+x) is on the LEFT
    assert cross_product_side((0, 0), (0, -10), (5, -5)) > 0
    assert cross_product_side((0, 0), (0, -10), (-5, -5)) < 0


def test_compass():
    assert compass(180) == "S" and compass(350) == "N" and compass(135) == "SE"


def test_dijkstra_and_astar_agree():
    adj, coords = build_highway(30)
    d_cost, d_path = dijkstra(adj, 0, 29, weight="time")
    a_cost, a_path = astar(adj, coords, 0, 29, weight="time")
    assert d_path == a_path == list(range(30))
    assert abs(d_cost - a_cost) < 1e-6


def test_one_way_respected():
    adj, coords = build_highway(5)
    adj[2] = [e for e in adj[2] if e[0] != 1]  # make 2 -> 1 forbidden
    cost, path = dijkstra(adj, 4, 0)
    assert path is None and cost == float("inf")


def test_route_chainage_and_time():
    adj, coords = build_highway(11)
    r = build_route(adj, coords, list(range(11)))
    assert r.chainage_m[0] == 0 and abs(r.total_m - 10_000) < 100
    assert abs(r.total_s / 60 - 10) < 0.2  # 10 km at 60 km/h


@pytest.mark.real_data
def test_real_route_panvel_to_panaji():
    from engine.graph_loader import GRAPH_PATH, load_graph
    from engine.routing import RoadGraph
    if not os.path.exists(GRAPH_PATH):
        pytest.skip("data/nh66.graphml not downloaded yet")
    g = RoadGraph(*load_graph())
    route = g.route((18.9894, 73.1175), (15.4909, 73.8278))
    assert route is not None
    print(f"\nPanvel -> Panaji: {route.total_m / 1000:.1f} km, "
          f"{route.total_s / 3600:.2f} h, {len(route.coords)} points")
    assert 450 < route.total_m / 1000 < 650
