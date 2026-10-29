"""Congestion rules + time-dependent routing tests (hand-made fixtures)."""
from datetime import date

import pytest

from engine.congestion import CongestionModel, in_date_range, in_time_window, parse_days
from engine.routing import astar, build_route, td_search
from tests.fixtures.mini_world import LAT0, LON0, STEP, build_highway

THU = date(2026, 10, 29)   # a Thursday, outside monsoon
SUN = date(2026, 11, 1)
JULY = date(2026, 7, 15)

# A town around highway km 10 and a ghat around km 30 on the fixture road
ZONES = [
    {"name": "Testpur", "kind": "town", "region": "konkan", "lat": LAT0 - 10 * STEP, "lon": LON0, "radius_m": 2500},
    {"name": "Test ghat", "kind": "ghat", "region": "konkan", "lat": LAT0 - 30 * STEP, "lon": LON0, "radius_m": 3000},
]


def rule(priority, name, group, zone, days, start, end, factor, dfrom=None, dto=None):
    return {"priority": priority, "rule_name": name, "rule_group": group, "zone_filter": zone,
            "days": days, "start_time": start, "end_time": end, "date_from": dfrom, "date_to": dto,
            "speed_factor": factor}


RULES = [
    rule(1, "Town morning peak", "town", "town", "Mon-Sat", "08:30", "11:00", 0.5),
    rule(3, "Weekly bazaar", "town", "town", "Sun", "09:00", "14:00", 0.6),
    rule(4, "Town daytime", "town", "town", "all", "07:00", "22:00", 0.75),
    rule(10, "Ghat at night", "ghat", "ghat", "all", "22:00", "06:00", 0.5),
    rule(11, "Ghat section", "ghat", "ghat", "all", "00:00", "24:00", 0.6),
    rule(20, "Open highway at night", "open_night", "open", "all", "23:00", "05:00", 1.1),
    rule(50, "Monsoon", "monsoon", "all", "all", "00:00", "24:00", 0.85, "06-01", "09-30"),
]


@pytest.fixture
def world():
    adj, coords = build_highway(41)
    return adj, coords, CongestionModel(ZONES, RULES, coords)


def test_parsers():
    assert parse_days("Mon-Sat") == {0, 1, 2, 3, 4, 5}
    assert parse_days("Fri,Sun") == {4, 6}
    assert parse_days("Sat-Mon") == {5, 6, 0}
    assert in_time_window(23 * 60 + 30, "22:00", "06:00") and not in_time_window(12 * 60, "22:00", "06:00")
    assert in_date_range(JULY, "06-01", "09-30") and not in_date_range(THU, "06-01", "09-30")
    assert in_date_range(date(2026, 9, 16), "2026-09-14", "2026-09-24")
    assert not in_date_range(date(2027, 9, 16), "2026-09-14", "2026-09-24")


def test_zones(world):
    _, _, m = world
    assert m.zone_of(10)[0] == "town"
    assert m.zone_of(30)[0] == "ghat"
    assert m.zone_of(20)[0] == "open"


def test_peak_beats_daytime_inside_group(world):
    _, _, m = world
    f, names = m.factor(10, 9 * 60, THU)            # Thursday 09:00 in town
    assert f == 0.5 and names == ["Town morning peak"]
    f, names = m.factor(10, 13 * 60, THU)
    assert f == 0.75 and names == ["Town daytime"]
    f, names = m.factor(10, 10 * 60, SUN)           # Sunday: bazaar, not the Mon-Sat peak
    assert f == 0.6 and names == ["Weekly bazaar"]


def test_ghat_night_replaces_ghat_section(world):
    _, _, m = world
    assert m.factor(30, 23 * 60, THU)[0] == 0.5     # not 0.5 x 0.6
    assert m.factor(30, 12 * 60, THU)[0] == 0.6


def test_groups_multiply_and_dates(world):
    _, _, m = world
    f, names = m.factor(30, 12 * 60, JULY)          # ghat + monsoon
    assert f == pytest.approx(0.6 * 0.85)
    assert set(names) == {"Ghat section", "Monsoon"}
    assert m.factor(30, 12 * 60 + 1440, THU)[0] == 0.6   # next day still works


def test_never_faster_than_posted_limit(world):
    _, _, m = world
    assert m.factor(20, 0, THU, has_maxspeed=False)[0] == 1.1   # 00:00 open road
    assert m.factor(20, 0, THU, has_maxspeed=True)[0] == 1.0


def test_time_dependent_route_slower_in_peak(world):
    adj, coords, m = world
    _, free_path = astar(adj, coords, 0, 40, weight="time")
    free = build_route(adj, coords, free_path)
    peak = build_route(adj, coords, free_path, 8 * 60 + 50, THU, m)     # in town at ~09:00
    noon = build_route(adj, coords, free_path, 12 * 60, THU, m)
    assert peak.total_s > noon.total_s > free.total_s
    assert peak.free_flow_s == pytest.approx(free.total_s)
    assert "Town morning peak" in peak.traffic


def test_td_astar_matches_td_dijkstra(world):
    adj, coords, m = world
    a_cost, a_path = td_search(adj, coords, 0, 40, 9 * 60, THU, m, use_heuristic=True)
    d_cost, d_path = td_search(adj, coords, 0, 40, 9 * 60, THU, m, use_heuristic=False)
    assert a_path == d_path and a_cost == pytest.approx(d_cost)
    # the search's own cost equals the route built from the same departure
    assert build_route(adj, coords, a_path, 9 * 60, THU, m).total_s == pytest.approx(a_cost)
