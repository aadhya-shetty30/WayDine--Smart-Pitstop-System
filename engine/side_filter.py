"""
Task B - Side-of-road filter.

Indian traffic drives on the LEFT, so a restaurant on the left of the
direction of travel can be entered directly. A restaurant on the right means
crossing oncoming traffic, i.e. a U-turn, unless the road graph offers a
legal route (flyover, service road, separate access road).

Side test: 2D cross product in UTM metres of
    (segment end - segment start) x (restaurant - segment start)
    > 0  -> LEFT,   < 0 -> RIGHT
"""
from engine.geometry import compass, cross_product_side, to_utm


def side_of_road(route_geom, proj, lat, lon):
    """Return ('left' | 'right', direction_of_travel_text)."""
    i = proj["seg"]
    a, b = route_geom.utm[i], route_geom.utm[i + 1]
    cross = cross_product_side(a, b, to_utm(lat, lon))
    side = "left" if cross >= 0 else "right"
    return side, f"heading {compass(proj['bearing'])}"


def check_side(side, detour, cfg):
    """
    Keep LEFT side (detour limits are checked separately in Task A).

    Keep RIGHT side only if the directed graph has a legal path within the
    detour limits that does NOT just cross our own carriageway: the
    restaurant's access node must be off our route (flyover / service road /
    opposite access) and Dijkstra must have found a path within limits.
    Returns a rejection reason or None.
    """
    if side == "left":
        return None
    within = (detour["detour_m"] <= cfg["max_detour_km"] * 1000
              and detour["detour_s"] <= cfg["max_detour_min"] * 60)
    if detour["on_route"] or not within:
        return "Opposite side, needs U-turn"
    return None
