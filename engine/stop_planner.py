"""
Smart Stop Planner - dynamic programming over restaurants sorted by chainage.

Goal: choose a sequence of stops that
  * has one MEAL stop inside every meal window the trip overlaps,
  * never drives longer than rest_interval_min without a stop
    (the counter resets at every stop),
  * minimises the total detour (and prefers places with verified hours).

DP formulation
  nodes  : START, candidate restaurants in chainage order, END
  state  : (node, next_meal_index, meal_stops_taken)
           -> lowest cost (ties: smallest delay), the accumulated delay D
              (stop durations + detours) and a back-pointer to rebuild the plan
  edge   : drive from stop i straight to stop j (no stop in between)
  cost   : detour_km of each stop
           + penalty_missed_meal_km per meal window that was skipped
           + penalty_rest_overdue_km per leg longer than rest_interval_min
           + penalty_unverified_hours_km per stop with unverified hours
Because later arrival times depend on earlier stops, D is carried in the
state: each stop shifts every later arrival by its duration, and opening
hours are re-checked at the shifted time. Penalties make the DP always
solvable and report what could not be covered instead of failing.

A meal stop also counts as a rest. If a rest would have been due within
merge_window_min, the stop is labelled 'both' (rest + meal merged).
"""
from engine import night_mode, time_rules
from engine.opening_hours import hhmm_to_min

START, END = -1, "END"


def fmt_clock(abs_minute):
    """1530 -> '01:30 (+1 day)'."""
    days, m = divmod(int(round(abs_minute)), 1440)
    text = f"{m // 60:02d}:{m % 60:02d}"
    return text + (f" (+{days} day)" if days else "")


def meal_occurrences(meal_rules, dep_min, horizon_min, min_overlap):
    """Meal windows (absolute minutes) that overlap the trip by at least min_overlap."""
    occ = []
    for day in range(3):
        for r in meal_rules:
            s = day * 1440 + hhmm_to_min(r["start"])
            e = day * 1440 + hhmm_to_min(r["end"])
            if min(e, horizon_min) - max(s, dep_min) >= min_overlap:
                occ.append({"meal": r["meal_type"], "start": s, "end": e})
    occ.sort(key=lambda m: m["start"])
    return occ


def thin_candidates(items, cfg, weekday):
    """
    Keep the best few places per chainage bucket so the DP stays small.
    Preference: open at the arrival time without stops, then verified hours,
    then smaller detour.
    """
    buckets = {}
    for it in items:
        b = int(it["chainage_km"] // cfg["planner_bucket_km"])
        buckets.setdefault(b, []).append(it)
    kept = []
    for group in buckets.values():
        group.sort(key=lambda it: (not time_rules.open_at(it["_row"], *time_rules.split_day(it["arrival_min"], weekday)),
                                   not it["_row"]["hours_verified"], it["detour_km"]))
        kept += group[: int(cfg["planner_per_bucket"])]
    kept.sort(key=lambda it: it["chainage_km"])
    return kept


def can_stop(item, abs_minute, weekday, ctx, force_night):
    """Re-check opening hours (and night safety) at the shifted arrival time."""
    wd, minute = time_rules.split_day(abs_minute, weekday)
    row = item["_row"]
    if not time_rules.open_at(row, wd, minute):
        return False
    if force_night or night_mode.is_night(minute, ctx.cfg):
        reason, _, _ = night_mode.apply_safety_rules(row, ctx.safety_rules, {"open_at_arrival": True})
        return reason is None
    return True


def plan_trip(reachable, route, departure, weekday, ctx, prefs=None, night=False):
    """Run the DP and return the timeline."""
    cfg = ctx.cfg
    prefs = prefs or {}
    dep = hhmm_to_min(departure)
    total_drive = route.total_s / 60
    rest_iv = cfg["rest_interval_min"]
    merge = cfg["merge_window_min"]
    meal_dur, rest_dur = cfg["meal_stop_min"], cfg["rest_stop_min"]

    cands = thin_candidates(reachable, cfg, weekday)
    # drive time from the start to each candidate (route time + half detour, no stops)
    base = [it["arrival_min"] - dep for it in cands]
    horizon = dep + total_drive + 4 * meal_dur + (total_drive // rest_iv + 1) * rest_dur
    meals = meal_occurrences(ctx.meal_rules, dep, horizon, meal_dur)
    M = len(meals)
    # Which meals are "required" is decided on the drive-only trip, so that
    # the delay of our own stops cannot create a new meal at the destination.
    drive_only_end = dep + total_drive

    def skip_meals(k, arrival_abs, cost):
        """Advance past meal windows that already closed before arrival_abs (penalised)."""
        while k < M and meals[k]["end"] <= arrival_abs:
            k += 1
            cost += cfg["penalty_missed_meal_km"]
        return k, cost

    # states[node] = {key: (cost, D, parent_key, stop_info)}
    states = {START: {(START, 0, 0): (0.0, 0.0, None, None)}}
    stop_ok = {}  # memo: (candidate, minute) -> can we stop there then?
    order = [START] + list(range(len(cands)))
    best_end = None

    for i in order:
        base_i = 0.0 if i == START else base[i]
        for key, (cost, D, _, _) in list(states.get(i, {}).items()):
            _, k, n_meal = key

            # ---- edge to END
            drive = total_drive - base_i
            c = cost + (cfg["penalty_rest_overdue_km"] if drive > rest_iv else 0)
            ke = k
            while ke < M:  # meals the trip overlapped but we never stopped for
                m = meals[ke]
                if min(m["end"], drive_only_end) - max(m["start"], dep) >= meal_dur:
                    c += cfg["penalty_missed_meal_km"]
                ke += 1
            if best_end is None or c < best_end[0]:
                best_end = (c, D, key)

            # ---- edges to later candidates
            first_beyond_done = False
            start_j = 0 if i == START else i + 1
            for j in range(start_j, len(cands)):
                drive = base[j] - base_i
                if drive < 0:
                    continue
                overdue = drive > rest_iv
                if overdue and first_beyond_done:
                    break
                arr = dep + base[j] + D
                k2, c2 = skip_meals(k, arr, cost)
                if overdue:
                    c2 += cfg["penalty_rest_overdue_km"]
                memo = (j, int(arr))
                if memo not in stop_ok:
                    stop_ok[memo] = can_stop(cands[j], arr, weekday, ctx, night)
                if not stop_ok[memo]:
                    continue
                if overdue:
                    # beyond the rest limit we allow only the first place that is
                    # actually open (penalised) - covers long stretches with nothing open
                    first_beyond_done = True
                c2 += cands[j]["detour_km"]
                if not cands[j]["_row"]["hours_verified"]:
                    c2 += cfg["penalty_unverified_hours_km"]
                extra = cands[j]["detour_min"]
                rest_due = drive >= rest_iv - merge

                options = []
                if k2 < M and meals[k2]["start"] <= arr < meals[k2]["end"]:
                    kind = "both" if rest_due else "meal"
                    options.append(((j, k2 + 1, n_meal + 1), D + meal_dur + extra,
                                    {"type": kind, "meal": meals[k2]["meal"], "occ": k2}))
                options.append(((j, k2, n_meal), D + rest_dur + extra,
                                {"type": "rest", "meal": "snacks", "occ": None}))
                for new_key, newD, info in options:
                    bucket = states.setdefault(j, {})
                    if new_key not in bucket or (c2, newD) < bucket[new_key][:2]:
                        bucket[new_key] = (c2, newD, key, info)

    # ---- rebuild the chosen stops by following back-pointers
    cost, D_end, key = best_end
    chosen = []
    while key is not None and key[0] != START:
        c, D, parent, info = states[key[0]][key]
        chosen.append((key[0], info, parent))
        key = parent
    chosen.reverse()

    timeline = [{"time": fmt_clock(dep), "type": "start", "name": "Departure",
                 "chainage_km": 0.0}]
    D = 0.0
    covered = set()  # indices into `meals`
    total_detour = 0.0
    for j, info, _ in chosen:
        it = cands[j]
        arr = dep + base[j] + D
        dur = meal_dur if info["type"] in ("meal", "both") else rest_dur
        if info["occ"] is not None:
            covered.add(info["occ"])
        items = time_rules.typical_items(it["_row"], info["meal"], prefs, ctx.templates,
                                         it["template_keys"])
        timeline.append({
            "time": fmt_clock(arr), "depart": fmt_clock(arr + dur + it["detour_min"]),
            "type": info["type"], "meal": info["meal"], "restaurant_id": it["restaurant"]["id"],
            "name": it["restaurant"]["name"], "chainage_km": it["chainage_km"],
            "detour_km": it["detour_km"], "side": it["side"],
            "hours_verified": it["restaurant"]["hours_verified"],
            "lat": it["restaurant"]["lat"], "lon": it["restaurant"]["lon"],
            "typical_items": items,
        })
        D += dur + it["detour_min"]
        total_detour += it["detour_km"]
    end_abs = dep + total_drive + D
    timeline.append({"time": fmt_clock(end_abs), "type": "end", "name": "Arrival",
                     "chainage_km": round(route.total_m / 1000, 2)})

    required = [i for i, m in enumerate(meals)
                if min(m["end"], drive_only_end) - max(m["start"], dep) >= meal_dur]
    missed = [i for i in required if i not in covered]

    # driving legs longer than the rest interval
    legs, prev = [], 0.0
    for j, _, _ in chosen:
        legs.append(base[j] - prev)
        prev = base[j]
    legs.append(total_drive - prev)
    return {
        "timeline": timeline,
        "meals_required": [meals[i]["meal"] for i in required],
        "meals_missed": [meals[i]["meal"] for i in missed],
        "overdue_legs": sum(1 for leg in legs if leg > rest_iv),
        "longest_leg_min": round(max(legs), 1),
        "total_detour_km": round(total_detour, 2),
        "arrival": fmt_clock(end_abs),
        "candidates_considered": len(cands),
        "rest_interval_min": rest_iv, "meal_stop_min": meal_dur,
        "dp_cost": round(cost, 2),
    }
