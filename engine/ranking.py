"""
Rule-based ranking (no ML): a transparent weighted score.

score =   w_detour         * (1 - detour_km / max_detour_km)     smaller detour is better
        + w_pref_match     * preference match (0..1)            veg + cuisine match
        + w_parking        * has_parking (verified)
        + w_hours_verified * hours_verified
        + w_24x7_night     * is_24x7, only at night
        + w_completeness   * share of fields verified in OSM (0..1)
        + w_safety_bonus   * number of night bonus rules matched
        + w_soon           * (1 - eta_min / soon_horizon_min), floored at 0
                             places you reach soon rank higher (useful while driving)

All weights come from the config table. Top N is picked with heapq.nlargest.
"""
import heapq

from engine.opening_hours import min_to_hhmm


def preference_score(rest, prefs, cuisine_hit):
    """0..1: half for veg fit, half for cuisine fit (neutral 0.5 when unknown / no preference)."""
    veg_pref = prefs.get("veg_pref", "any")
    if veg_pref == "any":
        veg = 1.0
    elif rest["veg_type"] == "unknown":
        veg = 0.5
    elif veg_pref == "veg":
        veg = 1.0 if rest["veg_type"] == "veg" else 0.7  # 'both' still has veg options
    else:
        veg = 1.0
    cuisine = 1.0 if cuisine_hit else (0.5 if not prefs.get("preferred_cuisines") else 0.0)
    return (veg + cuisine) / 2


def completeness(rest):
    """Fraction of the four enrichable fields that are real OSM facts, not rule-based guesses."""
    facts = [rest["hours_verified"], rest["veg_type"] != "unknown",
             rest["parking_verified"], rest["lighting_verified"]]
    return sum(1 for f in facts if f) / len(facts)


def score(item, prefs, cfg):
    """Compute the score for one accepted item (dict built by engine.pipeline)."""
    r = item["restaurant"]
    detour_ratio = min(1.0, item["detour_km"] / cfg["max_detour_km"]) if cfg["max_detour_km"] else 0
    parts = {
        "detour": cfg["w_detour"] * (1 - detour_ratio),
        "preference": cfg["w_pref_match"] * preference_score(r, prefs, item["cuisine_match"]),
        "parking": cfg["w_parking"] * (1 if r["has_parking"] and r["parking_verified"] else 0),
        "hours_verified": cfg["w_hours_verified"] * (1 if r["hours_verified"] else 0),
        "24x7_night": cfg["w_24x7_night"] * (1 if item["night"] and r["is_24x7"] else 0),
        "completeness": cfg["w_completeness"] * completeness(r),
        "soon": cfg["w_soon"] * max(0.0, 1 - item.get("eta_min", 0) / cfg["soon_horizon_min"]),
        "safety_bonus": cfg["w_safety_bonus"] * len(item.get("bonuses", [])) if item["night"] else 0,
    }
    return round(sum(parts.values()), 2), {k: round(v, 2) for k, v in parts.items()}


def _eta_text(minutes):
    """65 -> '1 h 05 min', 20 -> '20 min'."""
    minutes = int(round(minutes))
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


def explanation(item):
    """Short human-readable reason, e.g. 'Left side, 0.4 km detour, lunch at 13:10, hours verified'."""
    r = item["restaurant"]
    bits = [f"in {_eta_text(item.get('eta_min', 0))}",
            f"{item['side'].capitalize()} side", f"{item['detour_km']:.1f} km detour",
            f"{item['meal']} at {min_to_hhmm(item['arrival_min'])}"]
    bits.append("hours verified" if r["hours_verified"] else "hours unverified")
    if r["is_24x7"]:
        bits.append("open 24x7")
    if r["has_parking"] and r["parking_verified"]:
        bits.append("parking")
    return ", ".join(bits)


def rank(accepted, prefs, cfg):
    """Score every accepted item, return the top N (heapq.nlargest) with explanations."""
    for item in accepted:
        item["score"], item["score_parts"] = score(item, prefs, cfg)
        item["explanation"] = explanation(item)
    # tie-break on chainage so that the nearer place wins equal scores
    return heapq.nlargest(int(cfg["top_n"]), accepted,
                          key=lambda it: (it["score"], -it["chainage_km"]))
