"""Phase 4 tests: Task C time rules, opening-hours parser, veg filter, night safety."""
import pytest

from engine.opening_hours import hhmm_to_min, is_open, is_open_simple, parse_opening_hours
from engine.pipeline import evaluate
from engine.time_rules import meal_type_at
from tests.fixtures.mini_world import (build_highway, east, full_route, make_db,
                                       make_restaurant, road_graph)

MEALS = [
    {"meal_type": "breakfast", "start": "06:00", "end": "10:30"},
    {"meal_type": "lunch", "start": "12:00", "end": "15:30"},
    {"meal_type": "snacks", "start": "16:00", "end": "18:30"},
    {"meal_type": "dinner", "start": "19:30", "end": "23:00"},
]
MON, FRI, SAT, SUN = 0, 4, 5, 6


# ------------------------------------------------------------ opening hours
@pytest.mark.parametrize("text, day, time, expected", [
    ("24/7", SUN, "03:00", True),
    ("Mo-Su 08:00-23:00", MON, "07:59", False),
    ("Mo-Su 08:00-23:00", MON, "08:00", True),
    ("08:00-22:00", SAT, "21:59", True),
    ("Mo-Fr 09:00-18:00; Sa 10:00-14:00; Su off", SAT, "15:00", False),
    ("Mo-Fr 09:00-18:00; Sa 10:00-14:00; Su off", SAT, "11:00", True),
    ("Mo-Fr 09:00-18:00; Sa 10:00-14:00; Su off", SUN, "11:00", False),
    ("Mo-Sa 11:00-15:00,19:00-23:00", MON, "16:00", False),
    ("Mo-Sa 11:00-15:00,19:00-23:00", MON, "20:00", True),
    ("Mo-Fr 08:00-12:00, Sa 09:00-11:00", SAT, "10:00", True),
    ("Mo,We,Fr 08:00-12:00", 1, "09:00", False),
])
def test_opening_hours_parser(text, day, time, expected):
    h = parse_opening_hours(text)
    assert h["hours_verified"] is True
    assert is_open(h["schedule"], day, hhmm_to_min(time)) is expected


def test_closing_past_midnight():
    h = parse_opening_hours("Mo-Fr 18:00-02:00")
    assert is_open(h["schedule"], FRI, hhmm_to_min("23:30"))
    assert is_open(h["schedule"], SAT, hhmm_to_min("01:30"))   # Friday's late night
    assert not is_open(h["schedule"], SAT, hhmm_to_min("03:00"))
    assert not is_open(h["schedule"], SUN, hhmm_to_min("01:30"))  # no Saturday session
    # summary columns: opens > closes means closing after midnight
    assert is_open_simple("18:00", "02:00", hhmm_to_min("01:30"))
    assert not is_open_simple("18:00", "02:00", hhmm_to_min("02:30"))


def test_unparseable_hours_fall_back_to_config_default():
    h = parse_opening_hours("sunrise-sunset; Jan-Mar off", "07:00", "23:00")
    assert h["hours_verified"] is False
    assert (h["opens"], h["closes"]) == ("07:00", "23:00")
    assert parse_opening_hours(None)["hours_verified"] is False


def test_breakfast_at_0745():
    assert meal_type_at(hhmm_to_min("07:45"), MEALS) == "breakfast"
    assert meal_type_at(hhmm_to_min("13:10"), MEALS) == "lunch"
    assert meal_type_at(hhmm_to_min("11:00"), MEALS) is None


# ------------------------------------------------------------ pipeline level
N = 40


def _run(tmp_path, rests, **kw):
    adj, coords = build_highway(N)
    db = make_db(tmp_path, rests)
    res = evaluate(road_graph(adj, coords), db, full_route(adj, coords, N), **kw)
    acc = {it["restaurant"]["name"]: it for it in res["accepted"]}
    rej = {it["restaurant"]["name"]: it for it in res["rejected"]}
    return acc, rej


def test_breakfast_arrival_pipeline(tmp_path):
    # 15.5 km at 60 km/h = 15.5 min after a 07:30 departure -> ~07:45
    acc, _ = _run(tmp_path, [make_restaurant("Misal Corner", east(15, 50))], departure="07:30")
    item = acc["Misal Corner"]
    assert item["meal"] == "breakfast"
    assert item["arrival"].startswith("07:4")
    assert "breakfast at 07:4" in item["explanation"]


def test_closed_at_arrival_rejected(tmp_path):
    _, rej = _run(tmp_path, [make_restaurant("Lunch Only", east(10, 50), "11:30-15:00")],
                  departure="20:00")
    assert rej["Lunch Only"]["reason"] == "Closed at 20:10"


def test_veg_only_filter(tmp_path):
    rests = [
        make_restaurant("Pure Veg", east(10, 50), veg_type="veg"),
        make_restaurant("Fish Shack", east(12, 50), veg_type="nonveg"),
        make_restaurant("Mystery Diner", east(14, 50), veg_type="unknown"),
    ]
    acc, rej = _run(tmp_path, rests, departure="12:30", prefs={"veg_pref": "veg"})
    assert "Pure Veg" in acc
    assert rej["Fish Shack"]["reason"] == "Non-veg only"
    assert "Veg options not confirmed" in acc["Mystery Diner"]["warnings"]
    assert all(i["is_veg"] for i in acc["Pure Veg"]["typical_items"])


def test_night_rejects_verified_unlit_place(tmp_path):
    rests = [make_restaurant("Dark Dhaba", east(10, 50), "24/7", lit=False, lit_verified=True)]
    acc, rej = _run(tmp_path, rests, departure="22:30")
    assert rej["Dark Dhaba"]["reason"] == "Unlit road at night (OSM lit=no)"
    assert rej["Dark Dhaba"]["stage"] == "Night"


def test_night_keeps_unknown_lighting_with_warning(tmp_path):
    rests = [make_restaurant("Unknown Lights", east(10, 50), "24/7", lit_verified=False)]
    acc, _ = _run(tmp_path, rests, departure="22:30")
    assert "Lighting unknown" in acc["Unknown Lights"]["warnings"]
    assert acc["Unknown Lights"]["night"] is True


def test_unlit_place_fine_by_day(tmp_path):
    rests = [make_restaurant("Dark Dhaba", east(10, 50), "24/7", lit=False)]
    acc, _ = _run(tmp_path, rests, departure="12:30")
    assert "Dark Dhaba" in acc


def test_ranking_prefers_small_detour_and_verified_hours(tmp_path):
    rests = [
        make_restaurant("Close Verified", east(10, 30)),
        make_restaurant("Close Unverified", east(12, 30), opening_hours=None),
    ]
    adj, coords = build_highway(N)
    db = make_db(tmp_path, rests)
    res = evaluate(road_graph(adj, coords), db, full_route(adj, coords, N), departure="12:30")
    assert res["top"][0]["restaurant"]["name"] == "Close Verified"
    assert "hours verified" in res["top"][0]["explanation"]
