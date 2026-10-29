# WayDine — Project Context

## PROBLEM
Navigation apps suggest restaurants by proximity only, ignoring direction of travel, road side, detours, U-turns and arrival time. WayDine recommends food stops that are along the route, on the accessible side of the road, open at arrival, and serving the right meal for that time.

## HARD RULES
- No AI/ML models or AI APIs anywhere. Only data structures, graph algorithms, SQL/spatial queries and rule-based logic.
- Write Dijkstra and A* ourselves using heapq. OSMnx is used only to download road and restaurant data, never for shortest paths.
- All restaurant data is real, downloaded from OpenStreetMap. No fictional restaurants in the app.
- Any field not available in OSM is filled by documented rules and flagged as "unverified". Never present a guessed value as fact.
- All thresholds must live in the database config table, never hard-coded.
- Code must be simple, readable and well commented, because judges will review it.
- Every rejected restaurant must carry a clear human-readable reason.

## STACK
Python 3.11, FastAPI, OSMnx, Shapely, PyProj (EPSG:32643, UTM zone 43N for metre calculations), PostgreSQL 16 + PostGIS via docker-compose (postgis/postgis image), psycopg, pytest, plain HTML/CSS/JS with Leaflet and OpenStreetMap tiles.

Local fallback (agreed with the team): when PostgreSQL is not reachable (`WAYDINE_DB_URL` unset or down), the app uses a SQLite file `data/waydine.sqlite` with the same tables; the spatial ST_DWithin query is then done with Shapely in UTM metres. Config/thresholds still come from the `config` table in either backend.

## CORRIDOR
Mumbai (Panvel) to Goa (Panaji) along NH66. Indian traffic drives on the LEFT.

## FOLDER STRUCTURE
```
waydine/
  data/       nh66.graphml, nh66_restaurants.geojson, nh66_parking.geojson
  db/         schema.sql, fetch_osm_restaurants.py, seed.py
  engine/     graph_loader.py, routing.py, geometry.py, corridor.py, side_filter.py, time_rules.py,
              opening_hours.py, ranking.py, night_mode.py, stop_planner.py, food_gap.py
  api/        main.py
  frontend/   index.html, app.js, style.css
  tests/      fixtures/ (small hand-made test cases only, not used by the app)
  docker-compose.yml, requirements.txt, README.md, CLAUDE.md
```

## Conventions
- Run everything from the `waydine/` folder: `python -m db.seed`, `uvicorn api.main:app`, `pytest`.
- Side of road: cross product in UTM, positive = left (accessible side in India), negative = right.
