"""
Task A - Corridor filter: is the restaurant along the route, ahead of us,
and reachable with a small detour?

1. Spatial query: ST_DWithin(restaurant.geom, route LINESTRING, buffer_m)
   (PostGIS, or the Shapely fallback in db/database.py).
2. For each candidate: nearest route segment, projection point, chainage.
3. "Behind you" if the chainage is behind the vehicle's current position.
4. Detour with OUR Dijkstra on the directed road graph:

       detour = d(A -> R) + d(R -> B) - d(A -> B along the route)

   A = route node about detour_window_m before the projection point
   B = route node about detour_window_m after it
   R = the restaurant's entry point: its projection onto the nearest road
       edge u->v (so d(A -> R) = d(A -> u) + part of the edge, and
       d(R -> B) = rest of the edge + d(v -> B)), plus the short straight
       access from the road to the door (in and out).
   Using a window instead of the two immediately adjacent nodes keeps the
   formula exact even when a side road branches off a little before the place.

   Only two bounded Dijkstra runs per restaurant: one forward from A and one
   backward (on the reversed graph) from B.
"""
import bisect

from shapely.geometry import LineString

from engine.geometry import (GridIndex, bearing_deg, geom_from_utm, geom_to_utm,
                             project_point_to_segment, to_utm)
from engine.routing import dijkstra_tree


class RouteGeometry:
    """Pre-computed UTM points + spatial index for one route."""

    def __init__(self, route):
        self.route = route
        self.utm = [to_utm(lat, lon) for lat, lon in route.coords]
        self.index = GridIndex(cell_deg=0.005)
        for i, (lat, lon) in enumerate(route.coords):
            self.index.add(i, lat, lon)

    def project(self, lat, lon, window=6):
        """
        Find the nearest route segment to (lat, lon).

        Returns dict(seg, t, offset_m, chainage_m, cum_time_s, proj_latlon).
        We look up the nearest route vertex with the grid index, then test
        the few segments around it - much faster than scanning every segment.
        """
        r = self.route
        p = to_utm(lat, lon)
        nearest_i, _ = self.index.nearest(lat, lon)
        best = None
        lo = max(0, nearest_i - window)
        hi = min(len(self.utm) - 1, nearest_i + window)
        for i in range(lo, hi):
            t, _, dist = project_point_to_segment(p, self.utm[i], self.utm[i + 1])
            if best is None or dist < best[2]:
                best = (i, t, dist)
        i, t, dist = best
        seg_len = r.chainage_m[i + 1] - r.chainage_m[i]
        seg_time = r.cum_time_s[i + 1] - r.cum_time_s[i]
        (alat, alon), (blat, blon) = r.coords[i], r.coords[i + 1]
        return {
            "seg": i, "t": t, "offset_m": dist,
            "chainage_m": r.chainage_m[i] + t * seg_len,
            "cum_time_s": r.cum_time_s[i] + t * seg_time,
            "proj_latlon": (alat + t * (blat - alat), alon + t * (blon - alon)),
            "bearing": bearing_deg(alat, alon, blat, blon),
        }


def current_chainage(route_geom, current_latlon=None, current_km=None):
    """Vehicle position as chainage in metres (default: trip start)."""
    if current_km is not None:
        return float(current_km) * 1000
    if current_latlon is not None:
        return route_geom.project(*current_latlon)["chainage_m"]
    return 0.0


def nearest_edges(graph, lat, lon):
    """
    Road edges touching the node nearest to (lat, lon), each with the
    projection of the restaurant onto it.
    Returns a list of (u, v, length_m, time_s, t, perpendicular_m).
    """
    R, _ = graph.locator.nearest_node(lat, lon)
    p = to_utm(lat, lon)
    edges = [(R, e[0], e[1], e[2]) for e in graph.adj.get(R, ())]        # R -> v
    edges += [(e[0], R, e[1], e[2]) for e in graph.radj.get(R, ())]      # u -> R
    out = []
    for u, v, length, tt in edges:
        a, b = to_utm(*graph.coords[u]), to_utm(*graph.coords[v])
        t, _, perp = project_point_to_segment(p, a, b)
        out.append((u, v, length, tt, t, perp))
    return out


def time_at_chainage(route, chainage_m):
    """Cumulative drive time (s) at a chainage, interpolated between route points."""
    ch = route.chainage_m
    i = min(max(bisect.bisect_right(ch, chainage_m) - 1, 0), len(ch) - 2)
    seg = ch[i + 1] - ch[i]
    t = (chainage_m - ch[i]) / seg if seg else 0.0
    return route.cum_time_s[i] + t * (route.cum_time_s[i + 1] - route.cum_time_s[i])


def compute_detour(graph, route, proj, rest, cfg):
    """
    Detour for a stop at `rest` using our Dijkstra on the directed graph.

    Returns dict(detour_m, detour_s, access_m, entry_edge, on_route) where
    detour_m is inf if no legal path exists within the limit.
    on_route = the entry edge is part of our own road (route edge in either direction).
    """
    window = cfg["detour_window_m"]
    max_detour_m = cfg["max_detour_km"] * 1000
    chain = route.chainage_m

    # A: last route node at least `window` metres before the projection
    a_idx = max(0, bisect.bisect_right(chain, proj["chainage_m"] - window) - 1)
    # B: first route node at least `window` metres after it
    b_idx = min(len(chain) - 1, bisect.bisect_left(chain, proj["chainage_m"] + window))
    A, B = route.nodes[a_idx], route.nodes[b_idx]
    base_m = chain[b_idx] - chain[a_idx]
    base_s = route.cum_time_s[b_idx] - route.cum_time_s[a_idx]

    # Bounded searches: any path longer than base + limit is useless anyway
    limit = base_m + max_detour_m
    from_a = dijkstra_tree(graph.adj, A, max_cost=limit)    # d(A -> x)
    to_b = dijkstra_tree(graph.radj, B, max_cost=limit)     # d(x -> B), reversed graph
    access_speed = cfg["access_speed_kmph"] / 3.6           # m/s, slow driving to the door

    best = {"detour_m": float("inf"), "detour_s": float("inf"), "access_m": None,
            "entry_edge": None, "on_route": False}
    for u, v, length, tt, t, perp in nearest_edges(graph, rest["lat"], rest["lon"]):
        if u not in from_a or v not in to_b:
            continue  # no legal (one-way respecting) path through this edge
        d1 = from_a[u][0] + t * length          # A -> entry point on the edge
        d2 = (1 - t) * length + to_b[v][0]      # entry point -> B
        t1 = from_a[u][1] + t * tt
        t2 = (1 - t) * tt + to_b[v][1]
        detour_m = max(0.0, d1 + d2 + 2 * perp - base_m)
        if detour_m < best["detour_m"]:
            best = {
                "detour_m": detour_m,
                "detour_s": max(0.0, t1 + t2 + 2 * perp / access_speed - base_s),
                "access_m": perp, "entry_edge": (u, v),
                "on_route": u in route.index_of and v in route.index_of,
            }
    return best


def corridor_candidates(db, route, cfg):
    """
    Spatial query: restaurants within buffer_m of the route polyline.
    The polyline is simplified first (5 m tolerance in UTM metres, far below
    buffer_m) so the query does not carry tens of thousands of vertices.
    """
    line = geom_to_utm(LineString([(lon, lat) for lat, lon in route.coords]))
    line = geom_from_utm(line.simplify(5))
    coords = [(lat, lon) for lon, lat in line.coords]
    return db.restaurants_near_route(coords, cfg["buffer_m"])


def check_ahead(proj, vehicle_chainage_m):
    """Return a rejection reason if the place is behind the vehicle, else None."""
    if proj["chainage_m"] < vehicle_chainage_m:
        behind_km = (vehicle_chainage_m - proj["chainage_m"]) / 1000
        return f"Behind you ({behind_km:.1f} km back)"
    return None


def check_detour(det, cfg):
    """Return a rejection reason if the detour is above the config limits, else None."""
    if det["detour_m"] == float("inf"):
        return f"Detour too long: no road path within {cfg['max_detour_km']:g} km"
    km, minutes = det["detour_m"] / 1000, det["detour_s"] / 60
    if km > cfg["max_detour_km"]:
        return f"Detour too long: {km:.1f} km"
    if minutes > cfg["max_detour_min"]:
        return f"Detour too long: {minutes:.0f} min"
    return None
