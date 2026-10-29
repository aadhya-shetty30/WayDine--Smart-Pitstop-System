# WayDine 🍛🛣️

**Route-based highway restaurant recommendations for Mumbai (Panvel) → Goa (Panaji) on NH66.**

Navigation apps suggest food by *proximity*. WayDine suggests food stops that are
**along the route, ahead of you, on the accessible (left) side of the road, open
when you actually arrive, and serving the right meal for that time**. It also
plans all meal and rest stops for the whole trip and warns about long stretches
with nothing open.

No AI/ML anywhere: only graph algorithms, data structures, spatial SQL and
rule-based logic. All restaurant data is **real OpenStreetMap data**.

---

## Quick start

```bash
cd waydine
pip install -r requirements.txt
docker-compose up -d                       # PostgreSQL 16 + PostGIS
python -m engine.graph_loader              # 1. download NH66 road graph  -> data/nh66.graphml
python -m db.fetch_osm_restaurants         # 2. download real restaurants -> data/nh66_*.geojson
python -m db.seed                          # 3. enrich + load into the DB, prints data quality
uvicorn api.main:app --port 8000           # 4. open http://localhost:8000
```

Steps 1 and 2 cache their results in `data/`, so they only hit OpenStreetMap once.
If Overpass stays busy, the road loader fills missing stretches from the main
OSM API in small cached tiles, and the restaurant fetcher uses one light
Overpass query that returns only centre points and tags. Run it with
`--osm-api` to fall back to OSM API tiles instead.
The road download area is the union of a wide band around hand-picked towns
and a 1.5 km band around the real NH66 geometry. That geometry is the OSM route
relation 5828163, fetched from the OSM API and cached in
`data/nh66_relation.json`. The two downloads share OSM node ids, so they merge
into one graph, and only the largest strongly connected component is kept.
Public Overpass servers are often busy; the loader retries with exponential
backoff. You can add mirrors with
`WAYDINE_OVERPASS_URLS="https://overpass-api.de/api,https://overpass.kumi.systems/api"`.

**No Docker?** If PostgreSQL is not reachable, WayDine automatically falls back
to a SQLite file (`data/waydine.sqlite`) with the same tables. The `ST_DWithin`
corridor query is then reproduced with Shapely in UTM metres. Everything else,
including reading every threshold from the `config` table, is identical.
Point at another Postgres with `WAYDINE_DB_URL=postgresql://user:pass@host/db`.

Run the tests: `pytest` (fixture tests need no downloads; tests marked
`real_data` run once `data/nh66.graphml` exists).

Run the demo in the terminal: `python demo.py`.

### Deploy (Render.com, free)

1. On render.com: **New + → Blueprint**, connect this GitHub repo. `render.yaml` does the rest:
   it installs the requirements, seeds the SQLite DB from `data/` during the build, and starts uvicorn.
2. You get an HTTPS URL, so **Use my location** also works on phones.
3. The free plan sleeps after 15 min idle. The first request then takes about a minute to wake it.

---

## How a restaurant is judged

Every candidate goes through the filters in order. The first failure becomes
its human-readable rejection reason.

| Step | Module | Check | Example rejection |
|---|---|---|---|
| A | `engine/corridor.py` | `ST_DWithin(geom, route, buffer_m)`, projection onto nearest route segment → chainage | – |
| A | `engine/corridor.py` | chainage behind the vehicle | `Behind you (3.2 km back)` |
| B | `engine/side_filter.py` | cross product in UTM: + = left, − = right. Right side kept only if the directed graph has a legal path (flyover / service road) within limits | `Opposite side, needs U-turn` |
| A | `engine/corridor.py` | detour = d(A→R) + d(R→B) − d(A→B) with our Dijkstra | `Detour too long: 3.4 km` |
| C | `engine/time_rules.py` | veg preference, budget band | `Non-veg only` |
| C | `engine/time_rules.py` | arrival = departure + route time + ½ detour time; opening hours (incl. past midnight); meal window | `Closed at 23:40` |
| Night | `engine/night_mode.py` | `safety_rules` table, in priority order | `Unlit road at night (OSM lit=no)` |
| Rank | `engine/ranking.py` | weighted score, top 5 with `heapq.nlargest` | – |

Accepted places get an explanation such as
*"Left side, 0.4 km detour, lunch at 13:10, hours verified"*.

### Detour, precisely
`A` is the route node ~`detour_window_m` (1 km) **before** the restaurant's
projection point and `B` the route node ~1 km **after** it. `R` is the
restaurant's entry point: its projection onto the nearest road edge `u→v`.
Then `d(A→R) = d(A→u) + t·len(u,v)`, `d(R→B) = (1−t)·len(u,v) + d(v→B)`, plus
the straight access distance in and out. One bounded Dijkstra runs forward
from A, and one runs on the **reversed** graph from B. One-way roads are
respected, so a place on the opposite carriageway of a divided highway
naturally gets a huge detour (the U-turn).

---

## Feature → CS concept

| Feature | CS concept | Where |
|---|---|---|
| Traffic-aware route | **Time-dependent A\* / Dijkstra**: edge cost depends on the clock time you reach it | `engine/routing.py` `td_search` |
| Congestion | **Rule engine** over `congestion_rules` + `congestion_zones` (groups: best rule wins inside a group, groups multiply) | `engine/congestion.py` |
| Fastest route Panvel → Panaji | **Dijkstra** and **A\*** (admissible haversine heuristic), written by hand with `heapq` | `engine/routing.py` |
| Detour cost | **Bounded single-source Dijkstra** + Dijkstra on the **reversed graph** | `engine/routing.py`, `engine/corridor.py` |
| One-way roads | **Directed adjacency list** | `engine/graph_loader.py` |
| Snap a point to the road | **Spatial hashing** (grid index) | `engine/geometry.py` `GridIndex` |
| Restaurants near the route | **Spatial index** (PostGIS GIST + `ST_DWithin`) | `db/schema.sql`, `db/database.py` |
| Chainage | **Point-to-segment projection** + prefix sums | `engine/geometry.py`, `engine/routing.py` |
| Left / right of road | **2D cross product** in UTM 43N metres | `engine/side_filter.py` |
| Top 5 | **Heap** (`heapq.nlargest`) | `engine/ranking.py` |
| Smart stop plan | **Dynamic programming** over stops sorted by chainage | `engine/stop_planner.py` |
| Food gap alert | **Sliding window** over consecutive open stops | `engine/food_gap.py` |
| Night safety | **Rule engine** driven by a DB table, in priority order | `engine/night_mode.py` |
| Opening hours | **Tokenizer + parser** for OSM `opening_hours` | `engine/opening_hours.py` |
| Duplicate removal | Hash map of name → grid index (same name within 50 m) | `db/fetch_osm_restaurants.py` |

---

## Data: real OpenStreetMap, honest about gaps

`db/fetch_osm_restaurants.py` takes the A\* route, buffers it by 1 km (in
UTM metres), and downloads every `amenity = restaurant | fast_food | cafe |
food_court` plus `amenity = parking`. Polygons become centroids, unnamed places
are skipped, and the same name within 50 m counts as a duplicate.

OSM rarely has opening hours, diet info, parking or lighting for highway
dhabas. WayDine fills these gaps with **documented rules** and marks each
filled value as **unverified**. The UI shows an *unverified* badge on every
guessed field, and nothing guessed is presented as fact.

| Rule | Field | Rule | Verified when |
|---|---|---|---|
| R1 | opening hours | parse OSM `opening_hours`; missing or unparseable → config `default_opens`–`default_closes` (07:00–23:00) | OSM tag parsed |
| R2 | veg_type | `diet:vegetarian` only→veg, yes→both, no→nonveg; else cuisine contains "vegetarian" → veg; else `unknown` | tag present |
| R3 | cuisines | OSM `cuisine` split on `;`; missing → amenity type | tag present |
| R4 | cost band | fast_food, cafe → low; restaurant, food_court → medium | never (always an estimate) |
| R5 | parking | OSM parking within `parking_radius_m` (100 m) or `parking=*` / `capacity:car` tags | that evidence exists |
| R6 | lighting | `lit` tag on the nearest road edge (kept in the road graph) | `lit=yes/no` present |
| R7 | typical items | `menu_templates` by cuisine/type and meal (Maharashtrian, Malvani, South Indian, North Indian dhaba, cafe, fast food). Shown as **"Typical items"**, never as the real menu | never |

How rules are used:
* `veg_type = unknown` is **not rejected** for veg users. It shows *"Veg options not confirmed"*.
* Lighting at night: a place is rejected only when OSM explicitly says `lit=no`. Unknown lighting is kept with a *"Lighting unknown"* warning.
* Unverified hours lower the ranking score (`w_hours_verified`), and the stop planner prefers verified places.

`GET /data-quality` and the page header show how many places have each field verified.

---

## Configuration (all in the `config` table)

| key | default | meaning |
|---|---|---|
| buffer_m | 200 | corridor width for `ST_DWithin` |
| max_detour_km / max_detour_min | 2 / 5 | detour limits |
| detour_window_m | 1000 | distance of route nodes A/B before/after the place |
| access_speed_kmph | 20 | speed for the last metres from road to door |
| rest_interval_min / rest_stop_min / meal_stop_min | 120 / 15 / 30 | stop planner |
| merge_window_min | 20 | rest + meal merged if a rest is due within this |
| food_gap_km | 40 | gap alert threshold |
| night_start / night_end | 22:00 / 05:00 | automatic night mode |
| default_opens / default_closes | 07:00 / 23:00 | hours when OSM has none (unverified) |
| parking_radius_m | 100 | rule R5 |
| w_* | see `db/seed.py` | ranking weights |
| planner_*, penalty_* | see `db/seed.py` | DP candidate thinning and soft-constraint penalties |

Tables: `restaurants`, `menu_templates`, `meal_rules`, `users`, `config`,
`safety_rules`, `congestion_zones`, `congestion_rules` (see `db/schema.sql`).

---

## Traffic: rule-based congestion + time-dependent routing

No live-traffic API: the team agreed rules, stored in two tables.

**`congestion_zones`** – 18 towns on NH66 (2.5 km radius: Panvel … Mapusa, Porvorim, Panaji)
and 3 ghats (Kashedi 4 km, Bhoste 2 km, Parshuram 2 km). Centres are approximate and editable.

**`congestion_rules`** (speed factor, 0.5 = half speed):

| Group | Rule | Where | When | Factor |
|---|---|---|---|---|
| town | Town morning peak | towns | Mon–Sat 08:30–11:00 | 0.5 |
| town | Town evening peak | towns | daily 17:30–21:00 | 0.5 |
| town | Weekly bazaar | towns | Sun 09:00–14:00 | 0.6 |
| town | Town daytime | towns | daily 07:00–22:00 (otherwise) | 0.75 |
| ghat | Ghat at night | ghats | 22:00–06:00 | 0.5 |
| ghat | Ghat section | ghats | always | 0.6 |
| open_night | Open highway at night | outside towns & ghats | 23:00–05:00 | 1.1 (not above posted OSM limit) |
| trucks | Truck hours | outside towns | 22:00–06:00 | 0.85 |
| goa_rush | Goa weekend rush | Mapusa, Porvorim, Panaji | Fri 16–22, Sat 07–13 | 0.6 |
| monsoon | Monsoon | everywhere | 1 Jun – 30 Sep | 0.85 |
| festival | Ganeshotsav | everywhere | 14–24 Sep 2026 (**update yearly**) | 0.6 |

Inside a group the lowest-priority-number matching rule applies (peak beats daytime,
ghat-at-night replaces ghat); factors of different groups **multiply**
(ghat 0.6 × monsoon 0.85). The search is a **time-dependent A\***: an edge's time is
`free_flow / factor(zone, clock time, date)` at the moment the car reaches it, so the
same road costs more at 09:00 in Chiplun than at 14:00. Arrival times, opening-hours
checks, the stop plan and food gaps all use these traffic-aware times. The UI shows
e.g. *"+50 min traffic (free-flow 515 min): Town daytime +20, Town morning peak +20,
Ghat section +11"*. Turn it off with `congestion_enabled = 0` in `config`.
Detour times for single stops still use free-flow speeds.

## Live location

**📍 Use my location** (browser geolocation) sets the start to where you are and the
departure to *now*; **Live tracking** re-plans every 1 km or minute. Ranking adds
`w_soon` so places reachable in the next `soon_horizon_min` (120) minutes rank higher.
Location works on `http://localhost`; other devices need HTTPS. To demo away from
NH66: Chrome DevTools → ⋮ → More tools → Sensors → Location → e.g. `17.5330, 73.5150`.

## Smart Stop Planner (DP)

* **Nodes:** start, candidate restaurants sorted by chainage, end.
* **State:** `(last stop, next meal window, meals taken)`. It stores the lowest cost and the accumulated delay `D`.
* **Transition:** drive from stop *i* straight to stop *j*. The arrival at *j* is shifted by `D` (all earlier stop durations and detours), and opening hours and night safety are **re-checked at the shifted time**.
* **Cost:** total detour km, plus penalties (from config) for a missed meal window, a driving leg longer than `rest_interval_min`, or a stop with unverified hours. The penalties keep the DP solvable on sparse real data, and the result says exactly what could not be covered.
* A meal stop also counts as a rest. If a rest would be due within `merge_window_min`, the stop is labelled **both**.

## Food Gap Alert (sliding window)
Open places are sorted by chainage, with the trip start and destination as boundaries. A window of size 2 slides over the list, and every pair further apart than `food_gap_km` is flagged with start/end chainage, coordinates and the *last stop before the gap*.

---

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/route` | route summary (distance, time, coordinates) |
| POST | `/recommend` | ranked accepted places + rejected places with reasons |
| POST | `/plan-trip` | Smart Stop Planner timeline |
| POST | `/food-gaps` | gap segments |
| POST | `/trip` | all of the above in one call (used by the UI) |
| GET | `/restaurants/{id}` | details, typical items, verification flags |
| GET | `/data-quality` | verified vs unverified counts |
| GET | `/congestion-rules` | traffic rules and zones in use |

Example body:
```json
{"source": {"lat": 18.9894, "lon": 73.1175}, "destination": {"lat": 15.4909, "lon": 73.8278},
 "departure_time": "06:00", "night_mode": false,
 "preferences": {"veg_pref": "veg", "max_budget_band": "medium"}}
```

---

## Demo script (same route, two scenarios)

Open `http://localhost:8000` (or run `python demo.py` for a terminal version).

**Scenario 1: 06:00 departure.** From *Mumbai (Panvel)*, To *Goa (Panaji)*, Departure *06:00*, click **Find food stops**.
1. The blue line is the A\* route (509 km, 8.6 h of driving). Green dots are recommended places; numbered yellow markers are the top 5.
2. **Top 5 tab:** each card explains itself ("Left side, 0.3 km detour, breakfast at 07:52, hours verified").
3. Click a grey dot to show the rejection reason: *Opposite side, needs U-turn*, *Detour too long*, *Closed at …*.
4. **Trip plan tab:** breakfast, lunch, snacks and dinner stops plus rest stops. No drive is longer than 2 h, and "meal + rest" marks a merged stop.
5. Set Food = *Veg only* and resubmit. Non-veg-only places turn grey, and unknown ones show "Veg options not confirmed".

**Scenario 2: 21:30 departure.** Same route, Departure *21:30*, click **Find food stops**.
1. Night mode switches on **automatically** for arrivals after 22:00 (dark banner).
2. Places on roads OSM marks `lit=no` are greyed out with *Unlit road at night*. Places with unknown lighting stay, with a warning.
3. Many places are now *Closed at 23:40*. If the real data has a stretch longer than 40 km with nothing open, it is drawn **red** with a banner: *"Last open stop for 52 km: …"*.
4. Popups say **Source: OpenStreetMap** and badge every unverified field.

---

## Project layout

```
waydine/
  data/       nh66.graphml, nh66_restaurants.geojson, nh66_parking.geojson (downloaded caches)
  db/         schema.sql (+ schema_sqlite.sql fallback), database.py, fetch_osm_restaurants.py, seed.py
  engine/     graph_loader, routing, geometry, corridor, side_filter, time_rules, opening_hours,
              menu_rules, ranking, night_mode, stop_planner, food_gap, pipeline (glue)
  api/        main.py
  frontend/   index.html, app.js, style.css
  tests/      fixtures/ (hand-made test world only, never used by the app)
  demo.py, docker-compose.yml, requirements.txt
```

## Data quality (current snapshot, `python -m db.seed`)

| | count | share |
|---|---|---|
| Real OSM food places within 1 km of NH66 | 443 | |
| Opening hours from OSM | 13 | 3% |
| Veg info from OSM | 30 | 7% |
| Cuisine tagged | 74 | 17% |
| Parking verified (OSM parking within 100 m or tag) | 189 | 43% |
| Road lighting verified | 0 | 0% (only 34 of 25,400 road edges carry `lit=*`) |

Real-data results: at 06:00, 259 candidates are within 200 m (158 accepted,
101 rejected: 75 U-turn, 20 detour). The plan has breakfast at km 65, two
rest stops, and lunch + rest at km 405, with arrival at 16:06. At 21:30 almost
everything is closed after 23:00, so there is one dinner stop and then a
**431 km food gap** alert.

## Known limitations
* The PostGIS backend is written (`docker-compose.yml`, `db/schema.sql`, ST_DWithin query) but was only exercised through the SQLite fallback on the build machine.
* Because almost no OSM roads here have `lit` tags, night mode in practice keeps places with a "Lighting unknown" warning rather than rejecting them.
* OSM coverage of highway dhabas is incomplete, and most places lack hours, diet, parking and lighting tags. Rule-based defaults fill these and are flagged unverified.
* The graph has only motorway/trunk/primary roads. Right-side places reachable only through small service roads not in the graph are rejected as needing a U-turn.
* Traffic is rule-based (time of day, weekday, season, festival), not live. Zone centres are approximate.
* Opening hours support the common OSM formats only. Month/holiday rules and `sunrise` fall back to defaults.
* The stop planner thins candidates to the best few per 5 km (`planner_per_bucket`) to keep the DP fast.
