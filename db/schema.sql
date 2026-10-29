-- WayDine schema for PostgreSQL 16 + PostGIS.
-- Default rows (config, meal_rules, safety_rules, menu_templates) are inserted
-- by db/seed.py so the PostGIS and SQLite backends share one source of truth.

CREATE EXTENSION IF NOT EXISTS postgis;

DROP TABLE IF EXISTS restaurants, menu_templates, meal_rules, users, config, safety_rules,
    congestion_zones, congestion_rules CASCADE;

CREATE TABLE restaurants (
    id                SERIAL PRIMARY KEY,
    osm_id            TEXT UNIQUE,
    name              TEXT NOT NULL,
    amenity_type      TEXT NOT NULL,              -- restaurant / fast_food / cafe / food_court
    geom              GEOGRAPHY(Point, 4326) NOT NULL,
    opens             TIME,
    closes            TIME,                       -- closes < opens means past midnight
    is_24x7           BOOLEAN DEFAULT FALSE,
    hours_verified    BOOLEAN DEFAULT FALSE,      -- FALSE = default hours from config
    opening_hours_raw TEXT,                       -- original OSM opening_hours string
    hours_json        TEXT,                       -- parsed weekly schedule {weekday: [[start_min, end_min]]}
    veg_type          TEXT CHECK (veg_type IN ('veg', 'nonveg', 'both', 'unknown')),
    cuisines          TEXT[],
    avg_cost_band     TEXT CHECK (avg_cost_band IN ('low', 'medium', 'high')),
    has_parking       BOOLEAN DEFAULT FALSE,
    parking_verified  BOOLEAN DEFAULT FALSE,
    is_well_lit       BOOLEAN,                    -- NULL = unknown
    lighting_verified BOOLEAN DEFAULT FALSE,
    source            TEXT DEFAULT 'OpenStreetMap'
);

-- Spatial index so ST_DWithin only checks restaurants near the route
CREATE INDEX restaurants_geom_gist ON restaurants USING GIST (geom);

CREATE TABLE menu_templates (
    id               SERIAL PRIMARY KEY,
    cuisine_or_type  TEXT NOT NULL,               -- e.g. 'malvani', 'south_indian', 'fast_food'
    meal_type        TEXT CHECK (meal_type IN ('breakfast', 'lunch', 'snacks', 'dinner')),
    item_name        TEXT NOT NULL,
    is_veg           BOOLEAN NOT NULL,
    typical_price    INT
);

CREATE TABLE meal_rules (
    meal_type   TEXT PRIMARY KEY,
    start_time  TIME NOT NULL,
    end_time    TIME NOT NULL
);

CREATE TABLE users (
    id                  SERIAL PRIMARY KEY,
    name                TEXT,
    veg_pref            TEXT CHECK (veg_pref IN ('veg', 'nonveg', 'any')),
    preferred_cuisines  TEXT[],
    max_budget_band     TEXT CHECK (max_budget_band IN ('low', 'medium', 'high'))
);

CREATE TABLE config (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE safety_rules (
    priority          INT PRIMARY KEY,
    rule_name         TEXT NOT NULL,
    condition_column  TEXT NOT NULL,
    required_value    TEXT NOT NULL,
    action            TEXT CHECK (action IN ('require', 'bonus'))
);

CREATE TABLE congestion_zones (
    id        SERIAL PRIMARY KEY,
    name      TEXT NOT NULL,
    kind      TEXT CHECK (kind IN ('town', 'ghat')),
    region    TEXT,                               -- konkan / goa
    lat       DOUBLE PRECISION NOT NULL,
    lon       DOUBLE PRECISION NOT NULL,
    radius_m  INT NOT NULL
);

CREATE TABLE congestion_rules (
    priority      INT PRIMARY KEY,                -- lower = stronger inside its group
    rule_name     TEXT NOT NULL,
    rule_group    TEXT NOT NULL,                  -- one rule per group applies; groups multiply
    zone_filter   TEXT NOT NULL,                  -- all / town / ghat / open / not_town / goa_town
    days          TEXT NOT NULL,                  -- all, Mon-Sat, Sun, Fri ...
    start_time    TEXT NOT NULL,                  -- HH:MM
    end_time      TEXT NOT NULL,                  -- HH:MM (may wrap past midnight)
    date_from     TEXT,                           -- MM-DD or YYYY-MM-DD, NULL = any date
    date_to       TEXT,
    speed_factor  NUMERIC NOT NULL                -- 0.5 = half speed
);
