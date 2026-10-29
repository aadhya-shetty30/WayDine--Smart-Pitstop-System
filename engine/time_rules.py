"""
Task C - Time rules: arrival time, meal type, opening hours, preferences.

Times are handled as "minutes since midnight of the departure day", so a
value of 1530 means 01:30 the next day. The weekday is tracked alongside for
OSM day-specific opening hours.
"""
from engine.opening_hours import hhmm_to_min, is_open, is_open_simple, min_to_hhmm

BUDGET_ORDER = {"low": 0, "medium": 1, "high": 2}
SNACK_AMENITIES = ("cafe", "fast_food")


def arrival_minute(departure_min, cum_time_s, detour_s):
    """Arrival = departure + route time to projection point + half the detour time."""
    return departure_min + cum_time_s / 60 + (detour_s / 60) / 2


def split_day(abs_minute, start_weekday):
    """Absolute minute -> (weekday, minute_of_day)."""
    days, minute = divmod(int(abs_minute), 1440)
    return (start_weekday + days) % 7, minute


def meal_type_at(minute_of_day, meal_rules):
    """Return the meal whose window contains the time, or None."""
    for rule in meal_rules:
        if hhmm_to_min(rule["start"]) <= minute_of_day < hhmm_to_min(rule["end"]):
            return rule["meal_type"]
    return None


def open_at(rest, weekday, minute_of_day):
    """Opening-hours check: 24x7, per-day OSM schedule, or the opens/closes summary."""
    if rest.get("is_24x7"):
        return True
    if rest.get("schedule"):
        return is_open(rest["schedule"], weekday, minute_of_day)
    return is_open_simple(rest["opens"], rest["closes"], minute_of_day)


def check_hours_and_meal(rest, weekday, minute_of_day, meal_rules):
    """
    Returns (meal_type, reason). reason is None if the place is usable.

    Outside every meal window only snack places (cafe / fast food) and 24x7
    places are allowed, and the suggestion becomes 'snacks'.
    """
    clock = min_to_hhmm(minute_of_day)
    if not open_at(rest, weekday, minute_of_day):
        return None, f"Closed at {clock}"
    meal = meal_type_at(minute_of_day, meal_rules)
    if meal is None:
        if rest["amenity_type"] in SNACK_AMENITIES or rest.get("is_24x7"):
            return "snacks", None
        return None, f"No meal window at {clock} (only snack and 24x7 places)"
    return meal, None


def check_preferences(rest, prefs):
    """
    Veg preference and budget band. Returns (reason, warnings).
    Unknown veg_type is NOT rejected; it gets a warning instead.
    """
    warnings = []
    veg_pref = prefs.get("veg_pref", "any")
    vt = rest["veg_type"]
    if veg_pref == "veg":
        if vt == "nonveg":
            return "Non-veg only", warnings
        if vt == "unknown":
            warnings.append("Veg options not confirmed")
    elif veg_pref == "nonveg" and vt == "veg":
        return "Pure veg only", warnings

    max_band = prefs.get("max_budget_band") or "high"
    if BUDGET_ORDER[rest["avg_cost_band"]] > BUDGET_ORDER[max_band]:
        return f"Above budget ({rest['avg_cost_band']} cost band, estimated)", warnings
    return None, warnings


def cuisine_match(rest, prefs):
    """True if any preferred cuisine appears in the restaurant's cuisines."""
    wanted = {c.lower() for c in prefs.get("preferred_cuisines") or []}
    return bool(wanted & set(rest["cuisines"]))


def typical_items(rest, meal, prefs, templates, keys, limit=3):
    """
    'Typical items' for the meal from menu_templates (NOT the real menu -
    OSM has no menus). Veg users only see veg items.
    """
    veg_only = prefs.get("veg_pref") == "veg" or rest["veg_type"] == "veg"
    items = []
    for key in keys:
        for t in templates:
            if t["cuisine_or_type"] == key and t["meal_type"] == meal:
                if veg_only and not t["is_veg"]:
                    continue
                if rest["veg_type"] == "nonveg" and t["is_veg"] and prefs.get("veg_pref") == "nonveg":
                    continue
                items.append({"item": t["item_name"], "is_veg": t["is_veg"],
                              "typical_price": t["typical_price"]})
        if len(items) >= limit:
            break
    return items[:limit]
