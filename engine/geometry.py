"""
Geometry helpers used everywhere in WayDine.

- haversine_m: great-circle distance between two lat/lon points (metres)
- bearing_deg / compass: direction of travel along a road segment
- to_utm: project lat/lon to UTM zone 43N (EPSG:32643) so we can do flat x/y maths in metres
- project_point_to_segment: closest point on a segment (used for chainage)
- cross_product_side: which side of the road a point is on (left / right)
"""
import math

from pyproj import Transformer

EARTH_RADIUS_M = 6_371_000.0

# always_xy=True means we pass (lon, lat) and get back (x=easting, y=northing)
_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)
_FROM_UTM = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True)


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres between two (lat, lon) points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial compass bearing from point 1 to point 2 (0 = north, 90 = east)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass(bearing):
    """Turn a bearing in degrees into a compass word such as 'S' or 'SE'."""
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int((bearing + 22.5) // 45) % 8]


def to_utm(lat, lon):
    """Project (lat, lon) to UTM 43N metres. Returns (x, y)."""
    return _TO_UTM.transform(lon, lat)


def from_utm(x, y):
    """Inverse of to_utm. Returns (lat, lon)."""
    lon, lat = _FROM_UTM.transform(x, y)
    return lat, lon


def project_point_to_segment(p, a, b):
    """
    Project point p onto segment a-b (all (x, y) tuples in metres).

    Returns (t, (px, py), distance) where t in [0, 1] is how far along the
    segment the projection lies (0 = at a, 1 = at b).
    """
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        t = 0.0
    else:
        t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / seg_len_sq
        t = max(0.0, min(1.0, t))
    px, py = ax + t * dx, ay + t * dy
    return t, (px, py), math.hypot(p[0] - px, p[1] - py)


def cross_product_side(a, b, p):
    """
    2D cross product of (b - a) x (p - a), all in UTM metres.

    Positive -> p is on the LEFT of the direction a->b
    Negative -> p is on the RIGHT
    In India vehicles drive on the left, so LEFT is the easy-access side.
    """
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


class GridIndex:
    """
    Tiny spatial hash: buckets points into square lat/lon cells so that a
    'nearest point' lookup only scans the few cells around the query instead
    of every point. Avoids an O(N) scan per lookup.
    """

    def __init__(self, cell_deg=0.01):
        self.cell = cell_deg
        self.buckets = {}

    def _key(self, lat, lon):
        return (int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell)))

    def add(self, item, lat, lon):
        self.buckets.setdefault(self._key(lat, lon), []).append((item, lat, lon))

    def nearest(self, lat, lon, max_rings=50):
        """Return (item, distance_m) of the closest stored point, or (None, inf)."""
        ki, kj = self._key(lat, lon)
        best, best_d = None, float("inf")
        for ring in range(max_rings + 1):
            # scan the square ring of cells at Chebyshev distance `ring`
            for i in range(ki - ring, ki + ring + 1):
                for j in range(kj - ring, kj + ring + 1):
                    if max(abs(i - ki), abs(j - kj)) != ring:
                        continue
                    for item, plat, plon in self.buckets.get((i, j), ()):
                        d = haversine_m(lat, lon, plat, plon)
                        if d < best_d:
                            best, best_d = item, d
            # once something is found, one extra ring guarantees correctness
            # (a point in the next ring can't be closer than ring * cell size)
            if best is not None and best_d < ring * self.cell * 111_000 * 0.9:
                break
        return best, best_d


def geom_to_utm(geom):
    """Project a whole Shapely geometry (lon/lat) to UTM 43N metres."""
    import shapely
    return shapely.transform(geom, lambda x, y: _TO_UTM.transform(x, y), interleaved=False)


def geom_from_utm(geom):
    """Project a whole Shapely geometry from UTM 43N metres back to lon/lat."""
    import shapely
    return shapely.transform(geom, lambda x, y: _FROM_UTM.transform(x, y), interleaved=False)
