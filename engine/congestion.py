"""
Rule-based traffic congestion (no live traffic API, no ML).

Two DB tables drive everything:
  congestion_zones(name, kind [town/ghat], region [konkan/goa], lat, lon, radius_m)
  congestion_rules(priority, rule_name, rule_group, zone_filter, days,
                   start_time, end_time, date_from, date_to, speed_factor)

For a road edge entered at a given clock time and date:
  1. the edge's zone comes from its start node (town / ghat / open road)
  2. every rule whose zone, weekday, time window and date range match is a candidate
  3. inside one rule_group only the highest-priority (lowest number) rule counts
     e.g. 'town' group: morning peak 0.5 beats daytime 0.75
  4. the factors of different groups are MULTIPLIED
     e.g. ghat 0.6 x monsoon 0.85 = 0.51
  5. travel time = free-flow time / factor; a factor above 1 is not applied
     where OSM has a posted maxspeed (never faster than the limit)

zone_filter values: all, town, ghat, open (neither), not_town, goa_town
days values: all, Mon-Sat, Sun, Fri, Sat ... (comma separated, ranges allowed)
date_from / date_to: 'MM-DD' (every year) or 'YYYY-MM-DD' (one year), or empty
"""
from datetime import date as date_cls, timedelta

from engine.geometry import GridIndex, haversine_m
from engine.opening_hours import hhmm_to_min

DAY_NAMES = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def parse_days(text):
    """'Mon-Sat' -> {0..5}, 'Fri,Sun' -> {4, 6}, 'all' -> {0..6}."""
    if not text or text.lower() == "all":
        return set(range(7))
    out = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (DAY_NAMES[x.strip()] for x in part.split("-"))
            d = a
            out.add(d)
            while d != b:
                d = (d + 1) % 7
                out.add(d)
        else:
            out.add(DAY_NAMES[part])
    return out


def in_date_range(day, date_from, date_to):
    """day is a datetime.date; bounds are 'MM-DD' or 'YYYY-MM-DD' (inclusive)."""
    if not date_from or not date_to:
        return True
    if len(date_from) == 10:  # one specific year
        return date_from <= day.isoformat() <= date_to
    md = day.strftime("%m-%d")
    if date_from <= date_to:
        return date_from <= md <= date_to
    return md >= date_from or md <= date_to  # wraps over new year


def in_time_window(minute, start, end):
    """minute of day inside [start, end), windows may wrap past midnight."""
    s, e = hhmm_to_min(start), hhmm_to_min(end)
    if s <= e:
        return s <= minute < e
    return minute >= s or minute < e


class CongestionModel:
    """Loaded once per request from the DB; answers 'speed factor for this edge now'."""

    def __init__(self, zones, rules, coords):
        self.rules = []
        for r in sorted(rules, key=lambda r: r["priority"]):
            r = dict(r)
            r["day_set"] = parse_days(r["days"])
            self.rules.append(r)
        self.zones = zones
        self._node_zone = {}
        self._coords = coords
        self._index = GridIndex(cell_deg=0.02)
        for z in zones:
            self._index.add(z, z["lat"], z["lon"])
        self._memo = {}

    @property
    def max_factor(self):
        """Fastest possible factor - keeps the A* heuristic admissible."""
        return max([1.0] + [r["speed_factor"] for r in self.rules])

    def zone_of(self, node):
        """('town' | 'ghat' | 'open', region, zone name) for a graph node, cached."""
        if node not in self._node_zone:
            lat, lon = self._coords[node]
            best = ("open", None, None)
            # ghats win over towns when both cover a node (Parshuram ghat next to Chiplun)
            for z in sorted(self.zones, key=lambda z: z["kind"] != "ghat"):
                if haversine_m(lat, lon, z["lat"], z["lon"]) <= z["radius_m"]:
                    best = (z["kind"], z["region"], z["name"])
                    break
            self._node_zone[node] = best
        return self._node_zone[node]

    @staticmethod
    def _zone_matches(zone_filter, kind, region):
        return (zone_filter == "all"
                or zone_filter == kind
                or (zone_filter == "not_town" and kind != "town")
                or (zone_filter == "goa_town" and kind == "town" and region == "goa"))

    def factor(self, node, abs_minute, start_date, has_maxspeed=False):
        """
        Speed factor for an edge leaving `node`, entered at abs_minute
        (minutes since midnight of the departure day; may exceed 1440).
        Returns (factor, [rule names applied]).
        """
        kind, region, _ = self.zone_of(node)
        days, minute = divmod(int(abs_minute), 1440)
        bucket = minute // 5  # rules use whole minutes; 5-minute buckets keep the memo small
        key = (kind, region, start_date, days, bucket, has_maxspeed)
        if key in self._memo:
            return self._memo[key]

        day = start_date + timedelta(days=days)
        weekday = day.weekday()
        best_in_group = {}
        for r in self.rules:  # already in priority order
            if r["rule_group"] in best_in_group:
                continue
            if (weekday in r["day_set"]
                    and self._zone_matches(r["zone_filter"], kind, region)
                    and in_time_window(minute, r["start_time"], r["end_time"])
                    and in_date_range(day, r["date_from"], r["date_to"])):
                best_in_group[r["rule_group"]] = r

        f, names = 1.0, []
        for r in best_in_group.values():
            factor = r["speed_factor"]
            if factor > 1 and has_maxspeed:
                continue  # never faster than the posted limit
            f *= factor
            names.append(r["rule_name"])
        self._memo[key] = (f, names)
        return f, names


def load_model(db, coords):
    """Build the model from the DB, or None when congestion is switched off in config."""
    if not db.config().get("congestion_enabled", 0):
        return None
    return CongestionModel(db.congestion_zones(), db.congestion_rules(), coords)


def parse_date(text):
    return date_cls.fromisoformat(text) if text else date_cls.today()

