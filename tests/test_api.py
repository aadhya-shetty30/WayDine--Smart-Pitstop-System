"""Phase 6 tests: API endpoints on the fixture world (no real data needed)."""
import pytest
from fastapi.testclient import TestClient

import api.main as api
from tests.fixtures.mini_world import (LAT0, LON0, STEP, build_highway, east, make_db,
                                       make_restaurant, road_graph, west)

N = 121
# start / end of the fixture highway
ENDS = {"source": {"lat": LAT0, "lon": LON0}, "destination": {"lat": LAT0 - (N - 1) * STEP, "lon": LON0}}


def body(dep, **kw):
    return {**ENDS, "departure_time": dep, **kw}


@pytest.fixture
def client(tmp_path):
    adj, coords = build_highway(N)
    rests = [make_restaurant(f"Dhaba {km}", east(km, 60), "24/7", osm_id=f"t/{km}")
             for km in (5, 15, 25, 95, 105)]
    rests.append(make_restaurant("Wrong Side", west(50, 60), "24/7"))
    rests.append(make_restaurant("Dark Corner", east(60, 60), "24/7", lit=False))
    api.STATE["graph"] = road_graph(adj, coords)
    api.STATE["db"] = make_db(tmp_path, rests)
    api._route.cache_clear()
    return TestClient(api.app)  # not used as a context manager -> real-data startup skipped


def test_route(client):
    r = client.post("/route", json=body("06:00"))
    assert r.status_code == 200
    assert abs(r.json()["distance_km"] - 120) < 3


def test_recommend_has_accepted_and_reasons(client):
    res = client.post("/recommend", json=body("12:00")).json()
    names = [it["restaurant"]["name"] for it in res["accepted"]]
    assert "Dhaba 5" in names
    rej = {it["restaurant"]["name"]: it["reason"] for it in res["rejected"]}
    assert rej["Wrong Side"] == "Opposite side, needs U-turn"
    assert all(it["reason"] for it in res["rejected"])
    assert res["top"][0]["explanation"]
    assert res["top"][0]["restaurant"]["source"]


def test_night_trip_and_gap(client):
    res = client.post("/trip", json=body("21:30")).json()
    rej = {it["restaurant"]["name"]: it for it in res["rejected"]}
    assert rej["Dark Corner"]["stage"] == "Night"
    assert res["gaps"] and res["gaps"][0]["last_stop_before_gap"] == "Dhaba 25"
    assert res["plan"]["timeline"][0]["type"] == "start"


def test_plan_and_gaps_endpoints(client):
    assert client.post("/plan-trip", json=body("06:00")).status_code == 200
    gaps = client.post("/food-gaps", json=body("21:30")).json()
    assert gaps["food_gap_km"] == 40 and len(gaps["gaps"]) == 1


def test_restaurant_details_and_quality(client):
    rid = client.post("/recommend", json=body("12:00")).json()["accepted"][0]["restaurant"]["id"]
    d = client.get(f"/restaurants/{rid}").json()
    assert set(d["typical_items"]) == {"breakfast", "lunch", "snacks", "dinner"}
    assert "menu" in d["unverified"]
    q = client.get("/data-quality").json()
    assert q["total_restaurants"] == 7
    assert client.get("/restaurants/99999").status_code == 404


def test_point_far_from_corridor_rejected(client):
    far = {"source": {"lat": 10.0, "lon": 77.0}, "destination": ENDS["destination"], "departure_time": "06:00"}
    r = client.post("/route", json=far)
    assert r.status_code == 422 and "NH66 corridor" in r.json()["detail"]


def test_bad_time_rejected(client):
    assert client.post("/recommend", json=body("25:99")).status_code == 422


def test_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "WayDine" in r.text
