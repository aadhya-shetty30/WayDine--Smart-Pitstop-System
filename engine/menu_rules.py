"""
Rule R7 - which menu_templates apply to a restaurant.

OSM has no menus, so WayDine shows "Typical items" picked from the
menu_templates table by cuisine (first) and amenity type (fallback).
"""

# Maps OSM cuisine values to a menu_templates key (rule R7)
CUISINE_TO_TEMPLATE = {
    "indian": "north_indian", "north_indian": "north_indian", "punjabi": "north_indian",
    "regional": "maharashtrian", "maharashtrian": "maharashtrian", "vegetarian": "maharashtrian",
    "malvani": "malvani", "konkani": "malvani", "seafood": "malvani", "fish": "malvani",
    "goan": "malvani",
    "south_indian": "south_indian", "udupi": "south_indian",
    "chinese": "fast_food", "pizza": "fast_food", "burger": "fast_food", "sandwich": "fast_food",
    "coffee_shop": "cafe", "tea": "cafe", "coffee": "cafe", "juice": "cafe", "ice_cream": "cafe",
}
AMENITY_TO_TEMPLATE = {"restaurant": "restaurant", "food_court": "restaurant",
                       "fast_food": "fast_food", "cafe": "cafe"}


def template_keys(cuisines, amenity):
    """R7: menu_templates keys for a restaurant (cuisine matches first, then amenity)."""
    keys = [CUISINE_TO_TEMPLATE[c] for c in cuisines if c in CUISINE_TO_TEMPLATE]
    keys.append(AMENITY_TO_TEMPLATE.get(amenity, "restaurant"))
    return list(dict.fromkeys(keys))  # dedupe, keep order
