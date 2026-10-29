"""
Phase 1 - Our own shortest-path algorithms (no OSMnx / networkx routing).

Graph format (built by engine.graph_loader.to_adjacency):
    adj    = {node: [(neighbour, length_m, travel_time_s, attrs), ...]}
    coords = {node: (lat, lon)}

- dijkstra(): classic Dijkstra with a binary heap (heapq), optional cost cutoff
- astar():    A* with an admissible haversine heuristic
- nearest_node(): snap a lat/lon to the closest graph node (grid spatial hash)
- build_route(): route + per-point chainage (cumulative km) and cumulative time
"""
import heapq
from dataclasses import dataclass, field

from engine.geometry import GridIndex, haversine_m

# Weight selectors: index into the (neighbour, length, time, attrs) tuple
WEIGHT_INDEX = {"length": 1, "time": 2}

# Highest speed on our graph (motorway 80 km/h, but OSM maxspeed can be higher).
# Used to keep the A* time heuristic admissible (never over-estimates).
MAX_SPEED_MPS = 120 / 3.6


def _reconstruct(prev, src, dst):
    """Walk the predecessor map back from dst to src."""
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    path.reverse()
    return path


def dijkstra(adj, src, dst, weight="length", max_cost=float("inf")):
    """
    Shortest path from src to dst using Dijkstra's algorithm.

    max_cost lets callers stop early (e.g. detour checks only care about
    paths shorter than a limit). Returns (cost, path) or (inf, None).
    """
    w = WEIGHT_INDEX[weight]
    dist = {src: 0.0}
    prev = {}
    heap = [(0.0, src)]
    visited = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in visited:
            continue  # stale heap entry
        if u == dst:
            return d, _reconstruct(prev, src, dst)
        if d > max_cost:
            break  # everything left in the heap is even more expensive
        visited.add(u)
        for edge in adj.get(u, ()):
            v, cost = edge[0], edge[w]
            nd = d + cost
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))
    return float("inf"), None


def dijkstra_tree(adj, src, max_cost=float("inf")):
    """
    Single-source Dijkstra by length that stops at max_cost.
    Returns {node: (length_m, time_s)} for every node reached.
    Used for detours: one run from A gives d(A -> x) for all nearby x;
    one run on the REVERSED graph from B gives d(x -> B).
    """
    best = {src: (0.0, 0.0)}
    heap = [(0.0, 0.0, src)]
    done = {}
    while heap:
        d, t, u = heapq.heappop(heap)
        if u in done:
            continue
        if d > max_cost:
            break
        done[u] = (d, t)
        for v, length, tt, _ in adj.get(u, ()):
            nd = d + length
            if nd < best.get(v, (float("inf"),))[0]:
                best[v] = (nd, t + tt)
                heapq.heappush(heap, (nd, t + tt, v))
    return done


def reverse_adjacency(adj):
    """Same edges with direction flipped: radj[v] has (u, ...) for every u -> v."""
    radj = {}
    for u, edges in adj.items():
        for v, length, tt, attrs in edges:
            radj.setdefault(v, []).append((u, length, tt, attrs))
    return radj


def astar(adj, coords, src, dst, weight="time"):
    """
    A* search. Heuristic = straight-line (haversine) distance to dst,
    converted to seconds at MAX_SPEED when routing by time. Because a road
    can never be shorter than the straight line, the heuristic is admissible
    and A* returns the same optimal path as Dijkstra, while exploring fewer nodes.
    """
    w = WEIGHT_INDEX[weight]
    dlat, dlon = coords[dst]

    def h(n):
        lat, lon = coords[n]
        metres = haversine_m(lat, lon, dlat, dlon)
        return metres / MAX_SPEED_MPS if weight == "time" else metres

    g = {src: 0.0}
    prev = {}
    heap = [(h(src), 0.0, src)]
    closed = set()
    while heap:
        _, gu, u = heapq.heappop(heap)
        if u in closed:
            continue
        if u == dst:
            return gu, _reconstruct(prev, src, dst)
        closed.add(u)
        for edge in adj.get(u, ()):
            v, cost = edge[0], edge[w]
            ng = gu + cost
            if ng < g.get(v, float("inf")):
                g[v] = ng
                prev[v] = u
                heapq.heappush(heap, (ng + h(v), ng, v))
    return float("inf"), None


def td_search(adj, coords, src, dst, depart_min, start_date, model, use_heuristic=True):
    """
    TIME-DEPENDENT shortest path (fastest arrival) with congestion rules.

    The cost of an edge depends on WHEN we reach it:
        edge_time = free_flow_time / speed_factor(edge zone, clock time, date)
    use_heuristic=True  -> time-dependent A* (heuristic = straight line at the
                           fastest possible speed, so it stays admissible)
    use_heuristic=False -> time-dependent Dijkstra
    Because waiting never helps on a road (FIFO), the usual label-setting
    argument still holds and the first time we pop dst is optimal.
    Returns (seconds, path) or (inf, None).
    """
    dlat, dlon = coords[dst]
    fastest = MAX_SPEED_MPS * model.max_factor

    def h(n):
        if not use_heuristic:
            return 0.0
        lat, lon = coords[n]
        return haversine_m(lat, lon, dlat, dlon) / fastest

    g = {src: 0.0}
    prev = {}
    heap = [(h(src), 0.0, src)]
    closed = set()
    while heap:
        _, gu, u = heapq.heappop(heap)
        if u in closed:
            continue
        if u == dst:
            return gu, _reconstruct(prev, src, dst)
        closed.add(u)
        for v, _, tt, attrs in adj.get(u, ()):
            f, _ = model.factor(u, depart_min + gu / 60, start_date, attrs.get("has_maxspeed", False))
            ng = gu + tt / f
            if ng < g.get(v, float("inf")):
                g[v] = ng
                prev[v] = u
                heapq.heappush(heap, (ng + h(v), ng, v))
    return float("inf"), None


def path_cost(adj, path):
    """Total (length_m, time_s) of a node path."""
    length = time = 0.0
    for u, v in zip(path, path[1:]):
        edge = next(e for e in adj[u] if e[0] == v)
        length += edge[1]
        time += edge[2]
    return length, time


class NodeLocator:
    """Snaps lat/lon points to the nearest graph node using a GridIndex."""

    def __init__(self, coords, only_nodes=None):
        self.index = GridIndex(cell_deg=0.01)
        for n, (lat, lon) in coords.items():
            if only_nodes is None or n in only_nodes:
                self.index.add(n, lat, lon)

    def nearest_node(self, lat, lon):
        """Return (node_id, distance_m)."""
        return self.index.nearest(lat, lon)


@dataclass
class Route:
    """A computed route with per-point chainage and cumulative time."""
    nodes: list                      # graph node ids along the route
    coords: list                     # (lat, lon) per node
    chainage_m: list                 # cumulative distance at each point
    cum_time_s: list                 # cumulative travel time at each point
    total_m: float = 0.0
    total_s: float = 0.0
    index_of: dict = field(default_factory=dict)  # node id -> position in route
    free_flow_s: float = 0.0                       # same path without congestion
    traffic: dict = field(default_factory=dict)    # rule names -> extra seconds

    def to_dict(self):
        return {
            "distance_km": round(self.total_m / 1000, 2),
            "duration_min": round(self.total_s / 60, 1),
            "points": len(self.coords),
            "coords": [[round(a, 6), round(b, 6)] for a, b in self.coords],
            "chainage_km": [round(c / 1000, 3) for c in self.chainage_m],
            "cum_time_min": [round(t / 60, 2) for t in self.cum_time_s],
        }


def build_route(adj, coords, path, depart_min=None, start_date=None, model=None):
    """
    Turn a node path into a Route with chainage and cumulative time.
    With a congestion model, each edge's time uses the clock time at which
    the vehicle reaches it, and the extra time is recorded per rule.
    """
    chain, ctime = [0.0], [0.0]
    free = 0.0
    traffic = {}
    for u, v in zip(path, path[1:]):
        edge = next(e for e in adj[u] if e[0] == v)
        chain.append(chain[-1] + edge[1])
        tt = edge[2]
        free += tt
        if model is not None:
            f, names = model.factor(u, depart_min + ctime[-1] / 60, start_date,
                                    edge[3].get("has_maxspeed", False))
            if names:
                label = " + ".join(names)
                traffic[label] = traffic.get(label, 0.0) + (tt / f - tt)
            tt = tt / f
        ctime.append(ctime[-1] + tt)
    index_of = {}
    for i, n in enumerate(path):
        index_of.setdefault(n, i)
    return Route(nodes=path, coords=[coords[n] for n in path],
                 chainage_m=chain, cum_time_s=ctime,
                 total_m=chain[-1], total_s=ctime[-1], index_of=index_of,
                 free_flow_s=free, traffic=traffic)


def find_route(adj, coords, locator, src_latlon, dst_latlon, algorithm="astar",
               depart_min=None, start_date=None, model=None):
    """
    Snap both ends to the graph and return the fastest Route (or None).
    With a congestion model the search is time-dependent (depends on departure).
    """
    s, _ = locator.nearest_node(*src_latlon)
    t, _ = locator.nearest_node(*dst_latlon)
    if s is None or t is None:
        return None  # point is far away from every road in the graph
    if model is not None:
        _, path = td_search(adj, coords, s, t, depart_min, start_date, model,
                            use_heuristic=(algorithm != "dijkstra"))
    elif algorithm == "dijkstra":
        _, path = dijkstra(adj, s, t, weight="time")
    else:
        _, path = astar(adj, coords, s, t, weight="time")
    if path is None:
        return None
    return build_route(adj, coords, path, depart_min, start_date, model)


class RoadGraph:
    """Adjacency list + node coordinates + nearest-node locator, loaded once."""

    def __init__(self, adj, coords):
        self.adj = adj
        self.radj = reverse_adjacency(adj)  # for "distance TO a node" searches
        self.coords = coords
        self.locator = NodeLocator(coords)

    def route(self, src_latlon, dst_latlon, algorithm="astar", depart_min=None, start_date=None, model=None):
        return find_route(self.adj, self.coords, self.locator, src_latlon, dst_latlon, algorithm,
                          depart_min, start_date, model)
