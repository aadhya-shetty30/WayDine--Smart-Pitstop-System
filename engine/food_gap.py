"""
Food Gap Alert - sliding window over consecutive open places.

Take the places that are open at their arrival time, sort them by chainage,
add the route start and end as boundaries, then slide a window of size 2
over the list. Any pair further apart than food_gap_km (config) is a gap:
"Last open stop for 52 km".
"""
import bisect


def point_at_chainage(route, chainage_m):
    """(lat, lon) on the route at a given chainage (linear interpolation)."""
    ch = route.chainage_m
    i = min(max(bisect.bisect_right(ch, chainage_m) - 1, 0), len(ch) - 2)
    seg = ch[i + 1] - ch[i]
    t = (chainage_m - ch[i]) / seg if seg else 0.0
    (a1, o1), (a2, o2) = route.coords[i], route.coords[i + 1]
    return (a1 + t * (a2 - a1), o1 + t * (o2 - o1))


def route_slice(route, start_m, end_m, step=5):
    """Route coordinates between two chainages (every `step`-th point) for drawing."""
    ch = route.chainage_m
    i = bisect.bisect_left(ch, start_m)
    j = bisect.bisect_right(ch, end_m)
    pts = [point_at_chainage(route, start_m)] + route.coords[i:j:step] + [point_at_chainage(route, end_m)]
    return [[round(a, 6), round(b, 6)] for a, b in pts]


def find_food_gaps(open_items, route, cfg, start_km=0.0):
    """
    open_items: accepted places (open at arrival), each with 'chainage_km' and 'restaurant'.
    Returns a list of gap dicts.
    """
    limit_km = cfg["food_gap_km"]
    stops = sorted(open_items, key=lambda it: it["chainage_km"])
    # boundaries: where the vehicle is now and the destination
    points = ([{"km": start_km, "name": "Trip start"}]
              + [{"km": it["chainage_km"], "name": it["restaurant"]["name"]} for it in stops]
              + [{"km": route.total_m / 1000, "name": "Destination"}])

    gaps = []
    for prev, nxt in zip(points, points[1:]):  # sliding window of size 2
        length = nxt["km"] - prev["km"]
        if length > limit_km:
            gaps.append({
                "start_km": round(prev["km"], 2), "end_km": round(nxt["km"], 2),
                "length_km": round(length, 1),
                "start": point_at_chainage(route, prev["km"] * 1000),
                "end": point_at_chainage(route, nxt["km"] * 1000),
                "last_stop_before_gap": prev["name"], "next_stop_after_gap": nxt["name"],
                "message": f"Last open stop for {length:.0f} km: {prev['name']}",
                "coords": route_slice(route, prev["km"] * 1000, nxt["km"] * 1000),
            })
    return gaps
