"""
Night safety mode - a small rule engine driven by the safety_rules table.

Active when the arrival time is between night_start and night_end (config),
or when the user switches it on.

Rules are applied in priority order:
  require -> value must match, otherwise the place is rejected with a reason.
             If the value is UNKNOWN (not in OSM) the place is kept with a warning.
  bonus   -> value matches -> extra ranking score.
"""
from engine.opening_hours import hhmm_to_min

# Human-readable rejection / warning text per condition column
REJECT_TEXT = {
    "open_at_arrival": "Closed at arrival (night)",
    "is_well_lit": "Unlit road at night (OSM lit=no)",
    "has_parking": "No parking (night)",
    "hours_verified": "Opening hours not verified (night)",
    "is_24x7": "Not open 24x7 (night)",
}
UNKNOWN_TEXT = {
    "is_well_lit": "Lighting unknown",
    "has_parking": "Parking not confirmed",
}
# Which *_verified column says whether a value is a fact or a guess
VERIFIED_COLUMN = {"is_well_lit": "lighting_verified", "has_parking": "parking_verified"}


def is_night(minute_of_day, cfg):
    """True if the time falls in [night_start, night_end), wrapping past midnight."""
    start, end = hhmm_to_min(cfg["night_start"]), hhmm_to_min(cfg["night_end"])
    if start <= end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def _value(rest, column, facts):
    """Look up a rule column: computed facts first (open_at_arrival), then the row."""
    if column in facts:
        return facts[column]
    value = rest.get(column)
    verified_col = VERIFIED_COLUMN.get(column)
    if verified_col and not rest.get(verified_col):
        return None  # unknown - the value in the row is only a default
    return value


def apply_safety_rules(rest, rules, facts):
    """
    Returns (reject_reason or None, warnings list, bonus rule names list).
    facts: dict of computed values, e.g. {"open_at_arrival": True}.
    """
    warnings, bonuses = [], []
    for rule in rules:  # already sorted by priority
        col = rule["condition_column"]
        required = str(rule["required_value"]).lower() == "true"
        value = _value(rest, col, facts)
        if rule["action"] == "require":
            if value is None:
                warnings.append(UNKNOWN_TEXT.get(col, f"{rule['rule_name']} unknown"))
            elif bool(value) != required:
                return REJECT_TEXT.get(col, f"Fails safety rule: {rule['rule_name']}"), warnings, bonuses
        elif rule["action"] == "bonus" and value is not None and bool(value) == required:
            bonuses.append(rule["rule_name"])
    return None, warnings, bonuses
