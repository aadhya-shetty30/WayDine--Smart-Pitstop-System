-- SQLite fallback schema (used only when PostgreSQL/PostGIS is not reachable).
-- Same tables as schema.sql; geography becomes lat/lon columns and TEXT[]
-- becomes a JSON array string. Spatial filtering is done in Python with Shapely.

DROP TABLE IF EXISTS restaurants;
DROP TABLE IF EXISTS menu_templates;
DROP TABLE IF EXISTS meal_rules;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS config;
DROP TABLE IF EXISTS safety_rules;
DROP TABLE IF EXISTS congestion_zones;
DROP TABLE IF EXISTS congestion_rules;

CREATE TABLE restaurants (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    osm_id            TEXT UNIQUE,
    name              TEXT NOT NULL,
    amenity_type      TEXT NOT NULL,
    lat               REAL NOT NULL,
    lon               REAL NOT NULL,
    opens             TEXT,
    closes            TEXT,
    is_24x7           INTEGER DEFAULT 0,
    hours_verified    INTEGER DEFAULT 0,
    opening_hours_raw TEXT,
    hours_json        TEXT,
    veg_type          TEXT CHECK (veg_type IN ('veg', 'nonveg', 'both', 'unknown')),
    cuisines          TEXT,
    avg_cost_band     TEXT CHECK (avg_cost_band IN ('low', 'medium', 'high')),
    has_parking       INTEGER DEFAULT 0,
    parking_verified  INTEGER DEFAULT 0,
    is_well_lit       INTEGER,
    lighting_verified INTEGER DEFAULT 0,
    source            TEXT DEFAULT 'OpenStreetMap'
);
CREATE INDEX restaurants_latlon ON restaurants (lat, lon);

CREATE TABLE menu_templates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    cuisine_or_type  TEXT NOT NULL,
    meal_type        TEXT CHECK (meal_type IN ('breakfast', 'lunch', 'snacks', 'dinner')),
    item_name        TEXT NOT NULL,
    is_veg           INTEGER NOT NULL,
    typical_price    INTEGER
);

CREATE TABLE meal_rules (meal_type TEXT PRIMARY KEY, start_time TEXT NOT NULL, end_time TEXT NOT NULL);

CREATE TABLE users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT,
    veg_pref            TEXT CHECK (veg_pref IN ('veg', 'nonveg', 'any')),
    preferred_cuisines  TEXT,
    max_budget_band     TEXT CHECK (max_budget_band IN ('low', 'medium', 'high'))
);

CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE safety_rules (
    priority          INTEGER PRIMARY KEY,
    rule_name         TEXT NOT NULL,
    condition_column  TEXT NOT NULL,
    required_value    TEXT NOT NULL,
    action            TEXT CHECK (action IN ('require', 'bonus'))
);

CREATE TABLE congestion_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    kind TEXT CHECK (kind IN ('town', 'ghat')), region TEXT,
    lat REAL NOT NULL, lon REAL NOT NULL, radius_m INTEGER NOT NULL
);

CREATE TABLE congestion_rules (
    priority INTEGER PRIMARY KEY, rule_name TEXT NOT NULL, rule_group TEXT NOT NULL,
    zone_filter TEXT NOT NULL, days TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
    date_from TEXT, date_to TEXT, speed_factor REAL NOT NULL
);
