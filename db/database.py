"""
Database access for WayDine.

Primary backend: PostgreSQL 16 + PostGIS (docker-compose). The corridor query
uses ST_DWithin on a GIST-indexed GEOGRAPHY column.

Fallback backend: SQLite file data/waydine.sqlite, used automatically when
PostgreSQL is not reachable. It has the same tables; the ST_DWithin step is
reproduced with Shapely in UTM metres (bounding-box prefilter + exact distance).

Either way, every threshold is read from the `config` table.
"""
import json
import os
import sqlite3
from datetime import time

from shapely.geometry import LineString, Point

from engine.geometry import geom_to_utm, to_utm

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SQLITE_PATH = os.path.join(DATA_DIR, "waydine.sqlite")
PG_URL = os.environ.get("WAYDINE_DB_URL", "postgresql://waydine:waydine@localhost:5432/waydine")

RESTAURANT_COLUMNS = [
    "id", "osm_id", "name", "amenity_type", "lat", "lon", "opens", "closes",
    "is_24x7", "hours_verified", "opening_hours_raw", "hours_json", "veg_type",
    "cuisines", "avg_cost_band", "has_parking", "parking_verified",
    "is_well_lit", "lighting_verified", "source",
]
BOOL_COLUMNS = ["is_24x7", "hours_verified", "has_parking", "parking_verified",
                "is_well_lit", "lighting_verified"]


def _hhmm(value):
    """Normalise TIME / 'HH:MM:SS' / 'HH:MM' to 'HH:MM'."""
    if value is None:
        return None
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return str(value)[:5]


class Database:
    """Small repository object; `backend` is 'postgis' or 'sqlite'."""

    def __init__(self, force_sqlite=False, sqlite_path=SQLITE_PATH):
        self.backend = "sqlite"
        self.conn = None
        if not force_sqlite and os.environ.get("WAYDINE_FORCE_SQLITE") != "1":
            try:
                import psycopg
                self.conn = psycopg.connect(PG_URL, connect_timeout=2, autocommit=True)
                self.backend = "postgis"
            except Exception:
                self.conn = None
        if self.conn is None:
            os.makedirs(os.path.dirname(sqlite_path), exist_ok=True)
            self.conn = sqlite3.connect(sqlite_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        self.ph = "%s" if self.backend == "postgis" else "?"  # SQL placeholder

    # ------------------------------------------------------------------ helpers
    def _rows(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        if self.backend == "sqlite":
            self.conn.commit()

    def create_schema(self):
        name = "schema.sql" if self.backend == "postgis" else "schema_sqlite.sql"
        with open(os.path.join(os.path.dirname(__file__), name), encoding="utf-8") as f:
            sql = f.read()
        if self.backend == "postgis":
            self.conn.execute(sql)
        else:
            self.conn.executescript(sql)
            self.conn.commit()

    # ------------------------------------------------------------ config tables
    def config(self):
        """config table as a dict; numeric strings become floats."""
        out = {}
        for r in self._rows("SELECT key, value FROM config"):
            v = r["value"]
            try:
                out[r["key"]] = float(v)
            except ValueError:
                out[r["key"]] = v  # e.g. '22:00'
        return out

    def meal_rules(self):
        rows = self._rows("SELECT meal_type, start_time, end_time FROM meal_rules ORDER BY start_time")
        return [{"meal_type": r["meal_type"], "start": _hhmm(r["start_time"]),
                 "end": _hhmm(r["end_time"])} for r in rows]

    def safety_rules(self):
        return self._rows("SELECT priority, rule_name, condition_column, required_value, action "
                          "FROM safety_rules ORDER BY priority")

    def congestion_zones(self):
        return self._rows("SELECT name, kind, region, lat, lon, radius_m FROM congestion_zones")

    def congestion_rules(self):
        rows = self._rows("SELECT priority, rule_name, rule_group, zone_filter, days, start_time, "
                          "end_time, date_from, date_to, speed_factor FROM congestion_rules ORDER BY priority")
        for r in rows:
            r["speed_factor"] = float(r["speed_factor"])
        return rows

    def menu_templates(self):
        rows = self._rows("SELECT cuisine_or_type, meal_type, item_name, is_veg, typical_price "
                          "FROM menu_templates")
        for r in rows:
            r["is_veg"] = bool(r["is_veg"])
        return rows

    # -------------------------------------------------------------- restaurants
    def _clean(self, row):
        """Turn a DB row into a plain dict with Python types."""
        row = dict(row)
        row["opens"], row["closes"] = _hhmm(row["opens"]), _hhmm(row["closes"])
        for c in BOOL_COLUMNS:
            if row.get(c) is not None:
                row[c] = bool(row[c])
        if isinstance(row.get("cuisines"), str):
            row["cuisines"] = json.loads(row["cuisines"])
        row["cuisines"] = list(row.get("cuisines") or [])
        sched = json.loads(row["hours_json"]) if row.get("hours_json") else {}
        row["schedule"] = {int(k): [tuple(iv) for iv in v] for k, v in sched.items()}
        row.pop("hours_json", None)
        return row

    def _select_sql(self):
        if self.backend == "postgis":
            cols = [c for c in RESTAURANT_COLUMNS if c not in ("lat", "lon")]
            return ("SELECT " + ", ".join(cols) +
                    ", ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon FROM restaurants")
        return "SELECT " + ", ".join(RESTAURANT_COLUMNS) + " FROM restaurants"

    def all_restaurants(self):
        return [self._clean(r) for r in self._rows(self._select_sql())]

    def restaurant(self, rid):
        rows = self._rows(self._select_sql() + f" WHERE id = {self.ph}", (rid,))
        return self._clean(rows[0]) if rows else None

    def restaurants_near_route(self, route_coords, buffer_m):
        """
        All restaurants within buffer_m metres of the route polyline.
        PostGIS: ST_DWithin on geography (uses the GIST index).
        SQLite:  same result computed with Shapely in UTM metres.
        """
        if self.backend == "postgis":
            wkt = "LINESTRING(" + ", ".join(f"{lon} {lat}" for lat, lon in route_coords) + ")"
            sql = (self._select_sql() +
                   " WHERE ST_DWithin(geom, ST_GeogFromText(%s), %s)")
            return [self._clean(r) for r in self._rows(sql, (f"SRID=4326;{wkt}", buffer_m))]

        line = geom_to_utm(LineString([(lon, lat) for lat, lon in route_coords]))
        minx, miny, maxx, maxy = line.bounds
        out = []
        for r in self.all_restaurants():
            x, y = to_utm(r["lat"], r["lon"])
            # cheap bounding-box check first, then the exact distance
            if not (minx - buffer_m <= x <= maxx + buffer_m and miny - buffer_m <= y <= maxy + buffer_m):
                continue
            if line.distance(Point(x, y)) <= buffer_m:
                out.append(r)
        return out

    def insert_restaurant(self, r):
        """Insert one enriched restaurant dict (see db/seed.py)."""
        ph = self.ph
        cols = [c for c in RESTAURANT_COLUMNS if c not in ("id", "lat", "lon")]
        values = [r[c] for c in cols]
        if self.backend == "postgis":
            sql = (f"INSERT INTO restaurants ({', '.join(cols)}, geom) VALUES ("
                   + ", ".join([ph] * len(cols)) +
                   f", ST_GeogFromText({ph})) ON CONFLICT (osm_id) DO NOTHING")
            values.append(f"SRID=4326;POINT({r['lon']} {r['lat']})")
        else:
            values = [json.dumps(v) if isinstance(v, list) else v for v in values]
            sql = (f"INSERT OR IGNORE INTO restaurants ({', '.join(cols)}, lat, lon) VALUES ("
                   + ", ".join([ph] * (len(cols) + 2)) + ")")
            values += [r["lat"], r["lon"]]
        self.execute(sql, values)

    def data_quality(self):
        """Counts of verified vs unverified fields across all restaurants."""
        rows = self.all_restaurants()
        total = len(rows)

        def count(pred):
            return sum(1 for r in rows if pred(r))

        return {
            "total_restaurants": total,
            "source": "OpenStreetMap",
            "hours_verified": count(lambda r: r["hours_verified"]),
            "is_24x7": count(lambda r: r["is_24x7"]),
            "veg_info_known": count(lambda r: r["veg_type"] != "unknown"),
            "cuisine_tagged": count(lambda r: r["cuisines"] and r["cuisines"][0] != r["amenity_type"]),
            "parking_verified": count(lambda r: r["parking_verified"]),
            "lighting_verified": count(lambda r: r["lighting_verified"]),
            "lit_yes": count(lambda r: r["is_well_lit"] is True),
            "lit_no": count(lambda r: r["is_well_lit"] is False),
        }
