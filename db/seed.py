"""
Phase 2 - Seed the database with REAL OpenStreetMap restaurants.

Loads the cached GeoJSON (db/fetch_osm_restaurants.py), applies the documented
rule-based enrichment below, inserts everything into PostGIS (or the SQLite
fallback) and prints a data-quality summary.

ENRICHMENT RULES (every guessed field is flagged as unverified)
---------------------------------------------------------------
R1 hours      : OSM opening_hours parsed by engine/opening_hours.py.
                Missing/unparseable -> config default_opens..default_closes,
                hours_verified = False.
R2 veg_type   : OSM diet:vegetarian  only -> veg, yes -> both, no -> nonveg.
                Else cuisine tag contains "vegetarian" -> veg.
                Else 'unknown' (shown as "Veg options not confirmed").
R3 cuisines   : OSM cuisine tag split on ";". Missing -> [amenity_type]
                (e.g. "restaurant" = generic highway restaurant / dhaba).
R4 cost band  : fast_food, cafe -> low; restaurant, food_court -> medium.
                Always a rule-based estimate (OSM has no prices).
R5 parking    : OSM parking feature within parking_radius_m (config, 100 m)
                OR the place has an OSM parking tag (parking=*, capacity:car)
                -> has_parking = True, parking_verified = True.
                parking=no -> False, verified. Else False, parking_verified = False.
R6 lighting   : lit tag of the nearest road edge (graph built in Phase 1).
                lit=yes -> True verified, lit=no -> False verified,
                missing -> NULL (unknown), lighting_verified = False.
R7 menus      : OSM has no menus. "Typical items" come from menu_templates,
                matched by cuisine or amenity type (engine/menu_rules.py).

Run:  python -m db.seed
"""
import json

from db.database import Database
from db.fetch_osm_restaurants import fetch
from engine.geometry import GridIndex
from engine.graph_loader import load_graph
from engine.opening_hours import parse_opening_hours
from engine.routing import NodeLocator

# ------------------------------------------------------------- default data
DEFAULT_CONFIG = {
    # corridor / detour (Task A, B)
    "buffer_m": 200, "max_detour_km": 2, "max_detour_min": 5,
    "detour_window_m": 1000,     # route nodes A/B are taken this far before/after the place
    "access_speed_kmph": 20,     # speed for the last few metres from road node to the door
    "max_snap_km": 5,            # source/destination must be this close to the NH66 road graph
    # stop planner
    "rest_interval_min": 120, "rest_stop_min": 15, "meal_stop_min": 30, "merge_window_min": 20,
    # food gap alert
    "food_gap_km": 40,
    # stop planner DP: candidate thinning and soft-constraint penalties (in detour-km units)
    "planner_bucket_km": 5, "planner_per_bucket": 3,
    "penalty_missed_meal_km": 100, "penalty_rest_overdue_km": 50, "penalty_unverified_hours_km": 0.5,
    # night safety mode
    "night_start": "22:00", "night_end": "05:00",
    # default hours when OSM has none (flagged unverified)
    "default_opens": "07:00", "default_closes": "23:00",
    # enrichment
    "parking_radius_m": 100,
    # ranking weights (score = sum of weight * feature, see engine/ranking.py)
    "w_detour": 30, "w_pref_match": 15, "w_parking": 10, "w_hours_verified": 15,
    "w_24x7_night": 15, "w_completeness": 10, "w_safety_bonus": 5,
    "w_soon": 25, "soon_horizon_min": 120,   # places reached sooner rank higher (live use)
    # rule-based congestion + time-dependent routing (1 = on, 0 = off)
    "congestion_enabled": 1,
    # how many recommendations to return
    "top_n": 5,
}

MEAL_RULES = [
    ("breakfast", "06:00", "10:30"),
    ("lunch", "12:00", "15:30"),
    ("snacks", "16:00", "18:30"),
    ("dinner", "19:30", "23:00"),
]

# (priority, rule_name, condition_column, required_value, action)
SAFETY_RULES = [
    (1, "Open at arrival", "open_at_arrival", "true", "require"),
    (2, "Well-lit road", "is_well_lit", "true", "require"),  # only verified False rejects
    (3, "Parking available", "has_parking", "true", "bonus"),
    (4, "Verified opening hours", "hours_verified", "true", "bonus"),
    (5, "Open 24x7", "is_24x7", "true", "bonus"),
]

# Congestion zones along NH66: (name, kind, region, lat, lon, radius_m).
# Centres are approximate highway points near each town / ghat - edit freely.
CONGESTION_ZONES = [
    ("Panvel", "town", "konkan", 18.9894, 73.1175, 2500), ("Pen", "town", "konkan", 18.7370, 73.0960, 2500),
    ("Nagothane", "town", "konkan", 18.5400, 73.1330, 2500), ("Kolad", "town", "konkan", 18.4300, 73.2400, 2500),
    ("Mangaon", "town", "konkan", 18.2350, 73.2850, 2500), ("Mahad", "town", "konkan", 18.0830, 73.4180, 2500),
    ("Poladpur", "town", "konkan", 17.9850, 73.4650, 2500), ("Khed (Bharane Naka)", "town", "konkan", 17.7300, 73.3950, 2500),
    ("Chiplun", "town", "konkan", 17.5330, 73.5150, 2500), ("Sangameshwar", "town", "konkan", 17.1900, 73.5550, 2500),
    ("Lanja", "town", "konkan", 16.8600, 73.5450, 2500), ("Rajapur", "town", "konkan", 16.6550, 73.5200, 2500),
    ("Kankavli", "town", "konkan", 16.2700, 73.7100, 2500), ("Kudal", "town", "konkan", 16.0100, 73.6900, 2500),
    ("Sawantwadi bypass", "town", "konkan", 15.9000, 73.7700, 2500),
    ("Mapusa", "town", "goa", 15.5930, 73.8090, 2500), ("Porvorim", "town", "goa", 15.5300, 73.8250, 2500),
    ("Panaji", "town", "goa", 15.4909, 73.8278, 2500),
    ("Kashedi ghat", "ghat", "konkan", 17.8600, 73.4200, 4000),
    ("Bhoste ghat", "ghat", "konkan", 17.6800, 73.4200, 2000),
    ("Parshuram ghat", "ghat", "konkan", 17.5750, 73.5050, 2000),
]

# Congestion rules, agreed with the team:
# (priority, rule_name, rule_group, zone_filter, days, start, end, date_from, date_to, speed_factor)
# Inside one rule_group only the lowest priority number that matches applies;
# factors from different groups are multiplied.
CONGESTION_RULES = [
    (1, "Town morning peak", "town", "town", "Mon-Sat", "08:30", "11:00", None, None, 0.5),
    (2, "Town evening peak", "town", "town", "all", "17:30", "21:00", None, None, 0.5),
    (3, "Weekly bazaar", "town", "town", "Sun", "09:00", "14:00", None, None, 0.6),
    (4, "Town daytime", "town", "town", "all", "07:00", "22:00", None, None, 0.75),
    (10, "Ghat at night", "ghat", "ghat", "all", "22:00", "06:00", None, None, 0.5),
    (11, "Ghat section", "ghat", "ghat", "all", "00:00", "24:00", None, None, 0.6),
    (20, "Open highway at night", "open_night", "open", "all", "23:00", "05:00", None, None, 1.1),
    (30, "Truck hours", "trucks", "not_town", "all", "22:00", "06:00", None, None, 0.85),
    (40, "Goa weekend rush (Fri)", "goa_rush", "goa_town", "Fri", "16:00", "22:00", None, None, 0.6),
    (41, "Goa weekend rush (Sat)", "goa_rush", "goa_town", "Sat", "07:00", "13:00", None, None, 0.6),
    (50, "Monsoon", "monsoon", "all", "all", "00:00", "24:00", "06-01", "09-30", 0.85),
    # Ganeshotsav dates change every year - CHECK and update this row (2026 shown)
    (60, "Ganeshotsav", "festival", "all", "all", "00:00", "24:00", "2026-09-14", "2026-09-24", 0.6),
]

# Typical items: (template, meal_type, item, is_veg, typical_price INR)
MENU_TEMPLATES = [
    # Generic highway restaurant / dhaba
    ("restaurant", "breakfast", "Poha", True, 50), ("restaurant", "breakfast", "Misal Pav", True, 80),
    ("restaurant", "breakfast", "Omelette Pav", False, 70),
    ("restaurant", "lunch", "Veg Thali", True, 180), ("restaurant", "lunch", "Chicken Thali", False, 250),
    ("restaurant", "snacks", "Vada Pav", True, 30), ("restaurant", "snacks", "Kanda Bhaji", True, 60),
    ("restaurant", "dinner", "Dal Tadka & Jeera Rice", True, 160),
    ("restaurant", "dinner", "Chicken Curry & Roti", False, 260),
    # Maharashtrian
    ("maharashtrian", "breakfast", "Kanda Poha", True, 50), ("maharashtrian", "breakfast", "Sabudana Khichdi", True, 70),
    ("maharashtrian", "lunch", "Maharashtrian Veg Thali", True, 200), ("maharashtrian", "lunch", "Pithla Bhakri", True, 120),
    ("maharashtrian", "snacks", "Vada Pav", True, 30), ("maharashtrian", "snacks", "Thalipeeth", True, 80),
    ("maharashtrian", "dinner", "Bharli Vangi & Bhakri", True, 160), ("maharashtrian", "dinner", "Mutton Rassa", False, 320),
    # Malvani / Konkan coast
    ("malvani", "breakfast", "Amboli (rice pancake)", True, 60), ("malvani", "breakfast", "Usal Pav", True, 60),
    ("malvani", "lunch", "Fish Thali (Bangda/Surmai)", False, 300), ("malvani", "lunch", "Solkadhi & Rice Plate", True, 140),
    ("malvani", "snacks", "Kokum Sharbat & Bhaji", True, 60), ("malvani", "snacks", "Prawns Fry", False, 250),
    ("malvani", "dinner", "Malvani Chicken & Vade", False, 300), ("malvani", "dinner", "Kombdi Vade", False, 280),
    ("malvani", "dinner", "Veg Malvani Curry & Rice", True, 170),
    # South Indian / Udupi
    ("south_indian", "breakfast", "Idli Sambar", True, 50), ("south_indian", "breakfast", "Masala Dosa", True, 80),
    ("south_indian", "lunch", "South Indian Meals", True, 150), ("south_indian", "snacks", "Medu Vada", True, 50),
    ("south_indian", "snacks", "Filter Coffee", True, 30), ("south_indian", "dinner", "Uttapam", True, 90),
    # North Indian dhaba
    ("north_indian", "breakfast", "Aloo Paratha", True, 80), ("north_indian", "breakfast", "Chole Bhature", True, 110),
    ("north_indian", "lunch", "Dal Makhani & Naan", True, 200), ("north_indian", "lunch", "Butter Chicken & Naan", False, 300),
    ("north_indian", "snacks", "Samosa & Chai", True, 40), ("north_indian", "dinner", "Paneer Butter Masala & Roti", True, 220),
    ("north_indian", "dinner", "Chicken Tikka Masala", False, 300),
    # Cafe
    ("cafe", "breakfast", "Bun Maska & Chai", True, 40), ("cafe", "breakfast", "Veg Sandwich", True, 70),
    ("cafe", "lunch", "Grilled Sandwich", True, 100), ("cafe", "snacks", "Coffee & Cookies", True, 80),
    ("cafe", "dinner", "Pasta", True, 180),
    # Fast food
    ("fast_food", "breakfast", "Egg Sandwich", False, 60), ("fast_food", "breakfast", "Veg Burger", True, 80),
    ("fast_food", "lunch", "Veg Fried Rice", True, 120), ("fast_food", "lunch", "Chicken Burger", False, 140),
    ("fast_food", "snacks", "French Fries", True, 90), ("fast_food", "snacks", "Veg Frankie", True, 70),
    ("fast_food", "dinner", "Chicken Noodles", False, 150), ("fast_food", "dinner", "Veg Hakka Noodles", True, 120),
]


# --------------------------------------------------------- enrichment rules
def rule_veg_type(tags):
    """R2: diet:vegetarian, then cuisine, else unknown."""
    diet = (tags.get("diet:vegetarian") or "").lower()
    if diet == "only":
        return "veg"
    if diet == "yes":
        return "both"
    if diet == "no":
        return "nonveg"
    if "vegetarian" in (tags.get("cuisine") or "").lower():
        return "veg"
    return "unknown"


def rule_cuisines(tags):
    """R3: split cuisine on ';' or fall back to the amenity type."""
    raw = tags.get("cuisine")
    if raw:
        return [c.strip().lower() for c in raw.split(";") if c.strip()]
    return [tags.get("amenity") or "restaurant"]


def rule_cost_band(amenity):
    """R4: cost band from amenity type."""
    return "low" if amenity in ("fast_food", "cafe") else "medium"


def rule_parking(tags, lat, lon, parking_index, radius_m):
    """R5: returns (has_parking, parking_verified)."""
    p = (tags.get("parking") or "").lower()
    if p == "no":
        return False, True
    if p or tags.get("capacity:car"):
        return True, True
    _, d = parking_index.nearest(lat, lon, max_rings=2)
    if d <= radius_m:
        return True, True
    return False, False


def rule_lighting(lat, lon, locator, adj):
    """R6: lit tag of the road edges at the nearest graph node -> (is_well_lit, verified)."""
    node, _ = locator.nearest_node(lat, lon)
    lits = {e[3].get("lit") for e in adj.get(node, ())}
    if "yes" in lits:
        return True, True
    if "no" in lits:
        return False, True
    return None, False


def enrich(feature, cfg, parking_index, locator, adj):
    """Apply rules R1-R6 to one GeoJSON feature -> restaurant row dict."""
    tags = feature["properties"]
    lon, lat = feature["geometry"]["coordinates"]
    amenity = tags.get("amenity") or "restaurant"
    hours = parse_opening_hours(tags.get("opening_hours"), cfg["default_opens"], cfg["default_closes"])
    has_parking, parking_verified = rule_parking(tags, lat, lon, parking_index, cfg["parking_radius_m"])
    lit, lit_verified = rule_lighting(lat, lon, locator, adj)
    return {
        "osm_id": tags["osm_id"], "name": tags["name"].strip(), "amenity_type": amenity,
        "lat": lat, "lon": lon,
        "opens": hours["opens"], "closes": hours["closes"],
        "is_24x7": hours["is_24x7"], "hours_verified": hours["hours_verified"],
        "opening_hours_raw": tags.get("opening_hours"),
        "hours_json": json.dumps({str(k): v for k, v in hours["schedule"].items()}),
        "veg_type": rule_veg_type(tags), "cuisines": rule_cuisines(tags),
        "avg_cost_band": rule_cost_band(amenity),
        "has_parking": has_parking, "parking_verified": parking_verified,
        "is_well_lit": lit, "lighting_verified": lit_verified,
        "source": "OpenStreetMap",
    }


def validate(row):
    """Basic sanity checks; returns a rejection reason or None."""
    # Maharashtra + Goa bounding box
    if not (14.8 <= row["lat"] <= 22.1 and 72.6 <= row["lon"] <= 80.9):
        return "outside Maharashtra/Goa bounds"
    if not row["name"]:
        return "no name"
    return None


def seed_defaults(db):
    ph = db.ph
    for k, v in DEFAULT_CONFIG.items():
        db.execute(f"INSERT INTO config (key, value) VALUES ({ph}, {ph})", (k, str(v)))
    for m in MEAL_RULES:
        db.execute(f"INSERT INTO meal_rules VALUES ({ph}, {ph}, {ph})", m)
    for r in SAFETY_RULES:
        db.execute(f"INSERT INTO safety_rules VALUES ({ph}, {ph}, {ph}, {ph}, {ph})", r)
    for t in MENU_TEMPLATES:
        db.execute("INSERT INTO menu_templates (cuisine_or_type, meal_type, item_name, is_veg, typical_price) "
                   f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph})", t)
    for z in CONGESTION_ZONES:
        db.execute("INSERT INTO congestion_zones (name, kind, region, lat, lon, radius_m) "
                   f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})", z)
    for r in CONGESTION_RULES:
        db.execute(f"INSERT INTO congestion_rules VALUES ({', '.join([ph] * 10)})", r)
    db.execute(f"INSERT INTO users (name, veg_pref, max_budget_band) VALUES ({ph}, {ph}, {ph})",
               ("Demo traveller", "any", "medium"))


def main():
    db = Database()
    print(f"Database backend: {db.backend}")
    db.create_schema()
    seed_defaults(db)
    cfg = db.config()

    restaurants, parking = fetch()
    parking_index = GridIndex(cell_deg=0.002)
    for p in parking:
        lon, lat = p["geometry"]["coordinates"]
        parking_index.add(p["properties"]["osm_id"], lat, lon)

    adj, coords = load_graph()
    locator = NodeLocator(coords)

    loaded, rejected = 0, {}
    for f in restaurants:
        row = enrich(f, cfg, parking_index, locator, adj)
        reason = validate(row)
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        db.insert_restaurant(row)
        loaded += 1

    print(f"\nLoaded {loaded} restaurants, rejected {sum(rejected.values())} {rejected or ''}")
    q = db.data_quality()
    t = q["total_restaurants"] or 1
    print("\nDATA QUALITY SUMMARY (source: OpenStreetMap)")
    for key in ["hours_verified", "is_24x7", "veg_info_known", "cuisine_tagged",
                "parking_verified", "lighting_verified", "lit_yes", "lit_no"]:
        print(f"  {key:<18} {q[key]:>5} / {q['total_restaurants']}  ({100 * q[key] / t:.0f}%)")


if __name__ == "__main__":
    main()
