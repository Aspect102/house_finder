# House Finder — Bath student housing aggregator

Aggregates student housing listings for **Bath, UK** from multiple portals,
normalises them into one schema, de-duplicates, tracks **new / seen /
shortlisted / hidden** state, and serves a searchable web UI.

Enabled by default: **Rightmove, UniHomes, OpenRent, OnTheMarket, Zoopla**.
**SpareRoom** is implemented but opt-in, because it is room-oriented and
frequently serves an anti-bot challenge.

## Features

- **Pluggable source adapters** — HTTP scraping first, with a Playwright
  headless-browser fallback for JS-heavy or bot-protected pages (Zoopla needs
  it).
- **Cross-source search** — number of people/beds (default **6**), tenancy
  start date window, price range, area, furnished, bills included, property
  type, availability, keywords, and source.
- **Aggregation & state** — every listing is `new`, `seen`, `shortlisted` or
  `hidden`; new listings are flagged on each sync.
- **Sorting** — price, beds, first seen, tenancy start, source and area.
- **Saved searches** — save the current filters as a named search, apply it
  later, or let the scheduler run every enabled search on a timer.
- **Whole-property pricing** — per-person student rents (common on
  OnTheMarket/Zoopla) are converted to a whole-house monthly figure, and
  per-room "house share" listings are not counted as whole 6-bed houses.
- **Bus-route proximity** — filter listings by maximum walking distance to a
  stop on a chosen bus route (e.g. a First Bus **U1**–**U6** university route
  or The Big Lemon **20**). Route and stop data comes from the DfT **Bus Open
  Data Service (BODS)** and **NaPTAN**, cached locally and refreshed on a timer.
- **Scheduler** — internal APScheduler interval, or run one-shot syncs from
  cron/systemd.
- **Notifications** — email (SMTP) and webhook (Discord/Slack) for new matches.
- **REST API + HTMX web UI**.

## Quick start

```bash
cd ~/Documents/house_finder
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Required for Zoopla (and the browser fallback):
playwright install chromium

cp .env.example .env          # edit paths/credentials as needed
uvicorn house_finder.app:app --reload
```

Open <http://127.0.0.1:8000>. Use **Sync now** in the UI, or run a sync from
the CLI:

```bash
python -m house_finder.cli sync --beds 6 --tenancy-start-from 2027-06-01
```

Optional demo data (no network): `python -m house_finder.seed`. Do not seed a
production database — demo rows point at `example.com`.

## Saved searches

1. Set the filters in the sidebar.
2. Enter a name under **Saved searches** and click **Save current filters**.
3. **Run** executes that search immediately; clicking the name re-applies its
   filters in the UI.
4. When the scheduler runs, it executes every enabled saved search (or the
   default query if none are saved).

The same functionality is available over the API:

```bash
curl -s localhost:8000/api/profiles
curl -s -X POST localhost:8000/api/profiles \
  -H 'content-type: application/json' \
  -d '{"name":"6-bed July 2027","params":{"min_beds":6,"max_price":6000,"tenancy_start_from":"2027-07-01","tenancy_start_to":"2027-09-30"}}'
curl -s -X POST localhost:8000/api/profiles/1/run
```

CLI equivalents:

```bash
python -m house_finder.cli profiles list
python -m house_finder.cli profiles add --name "6-bed 2027" --beds 6 --max-price 6000 \
  --tenancy-start-from 2027-07-01 --tenancy-start-to 2027-09-30
python -m house_finder.cli profiles run --name "6-bed 2027"
python -m house_finder.cli profiles run          # all enabled searches
```

## Bus routes (BODS)

The sidebar can filter to listings within a walking distance of a stop on one
or more chosen bus routes. Route and stop data comes from the DfT **Bus Open
Data Service** timetable datasets (TransXChange), with stop coordinates from
**NaPTAN**. It is downloaded on a schedule and cached in the database — never
queried live per search.

1. Get a free API key at <https://data.bus-data.dft.gov.uk/account/> and set
   `HF_BODS_API_KEY` in `.env`.
2. Choose the routes to track with `HF_BODS_ROUTES` (default
   `U1,U1N,U2,U3,U4,U6,20@Big Lemon`). A bare line name matches any operator;
   `LINE@OPERATOR` restricts by a case-insensitive operator substring (needed
   because both First Bus and The Big Lemon publish a line "20").
3. Populate the data once:

   ```bash
   python -m house_finder.cli transit sync     # download routes/stops
   python -m house_finder.cli transit routes   # list what was found
   python -m house_finder.cli transit geocode  # fill coordinates from postcodes
   ```

   `transit sync` also back-fills coordinates for listings that have a postcode
   but no latitude/longitude (via postcodes.io), so `transit geocode` is only
   needed if you add listings another way.

4. In the UI, choose a **Mode**:
   - **Only listings near a stop on selected routes** (filter) — pick one or
     more routes and only listings within the **Max walk** of a stop on them
     are shown; each card shows its nearest such stop.
   - **All listings, label nearest routes** (annotate) — every listing is
     shown and each card lists the closest stop on *every* route within range,
     e.g. `U1 at Laura Place · 634 m` and `U2 at John's Court · 300 m`.

   **Max walk** can be given in metres or as a walking time (the two are
   linked at ≈4.8 km/h / 80 m per minute); editing either re-runs the search.
   There is also a **↻ Refresh bus data** button in the sidebar.

The scheduler refreshes route/stop data every `HF_BODS_REFRESH_HOURS` (default
weekly) and once at startup if no data is cached. Stops are restricted to the
configured ATCO administrative areas (`HF_BODS_ADMIN_AREAS`, default `018`
Bath & North East Somerset) so a same-numbered route from another city cannot
bleed in. First Bus does not currently publish a Bath **U1N** timetable, so it
will not appear even though it is listed by default.

## Running as a service

### systemd (internal scheduler, recommended)

```bash
sudo cp deploy/house-finder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now house-finder
systemctl status house-finder
journalctl -u house-finder -f
```

Edit `User=`, `Group=` and paths in the unit if your install differs. The
service runs the web app **and** the scheduler in one process
(`HF_SCHEDULER_ENABLED=true`).

### systemd timer (external schedule)

Set `HF_SCHEDULER_ENABLED=false`, then install the supplied one-shot service
and timer:

```bash
sudo cp deploy/house-finder.service deploy/house-finder-sync.service \
        deploy/house-finder-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now house-finder house-finder-sync.timer
systemctl list-timers house-finder-sync.timer
```

### cron

```cron
# every 3 hours
0 */3 * * * cd /home/housefinder/Documents/house_finder && .venv/bin/python -m house_finder.cli sync >> /var/log/house-finder.log 2>&1
```

## REST API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness and version |
| `GET` | `/api/sources` | Enabled/available adapters |
| `GET` | `/api/stats` | Counts by status and source |
| `GET` | `/api/listings` | Search listings with the same filters as the UI |
| `GET` | `/api/transit/routes` | Cached bus routes (for the route picker) |
| `POST` | `/api/transit/sync` | Refresh BODS routes/stops (`?force=true` re-downloads NaPTAN) |
| `GET` | `/api/profiles` | List saved searches |
| `POST` | `/api/profiles` | Create/update a saved search |
| `DELETE` | `/api/profiles/{id}` | Delete a saved search |
| `POST` | `/api/profiles/{id}/run` | Run a saved search now |

## Configuration

All settings come from environment variables / `.env` (see `.env.example`).
Key ones:

| Variable | Default | Meaning |
|---|---|---|
| `HF_DATABASE_URL` | `sqlite:///./house_finder.db` | DB location |
| `HF_SCHEDULER_ENABLED` | `true` | Run internal timer |
| `HF_SYNC_INTERVAL_MINUTES` | `180` | Scrape interval |
| `HF_SYNC_ON_STARTUP` | `false` | Run a sync at startup |
| `HF_DEFAULT_BEDS` | `6` | Default bed count in UI |
| `HF_SOURCES` | `rightmove,unihomes,openrent,onthemarket,zoopla` | Comma-separated adapters |
| `HF_MAX_RESULTS_PER_SOURCE` | `60` | Cap per source per sync |
| `HF_MAX_PAGES_PER_SOURCE` | `6` | Result pages fetched per source |
| `HF_RESPECT_ROBOTS` | `true` | Honour robots.txt when available |
| `HF_USE_PLAYWRIGHT_FALLBACK` | `true` | Browser fallback for JS/bot-protected pages |
| `HF_BODS_API_KEY` | – | Bus Open Data Service API key (bus-route filter) |
| `HF_BODS_ROUTES` | `U1,U1N,U2,U3,U4,U6,20@Big Lemon` | Lines to track (`LINE` or `LINE@OPERATOR`) |
| `HF_BODS_ADMIN_AREAS` | `018` | ATCO areas used to keep stops local |
| `HF_BODS_REFRESH_HOURS` | `168` | Bus-route refresh interval |
| `HF_DEFAULT_MAX_STOP_DISTANCE_M` | `800` | Default max walk to a stop |
| `HF_GEOCODE_ENABLED` | `true` | Fill missing coordinates via postcodes.io |
| `HF_SMTP_*`, `HF_WEBHOOK_URL` | – | Notification config |

Comma-separated values rely on `enable_decoding=False` in
`house_finder/config.py`; leave it in place or pydantic will try to JSON-decode
`HF_SOURCES`.

## Tests

```bash
HF_DATABASE_URL=sqlite:///./test_house_finder.db HF_SCHEDULER_ENABLED=false \
  python -m pytest -q
```

The suite covers normalisation, date filters, the pipeline, saved searches,
stats/defaults, the bus-route (TransXChange/NaPTAN/distance) feature, and
offline parser fixtures for every adapter.

## Adding a source

Create a module in `house_finder/sources/` subclassing `BaseSource`, implement
`fetch(query) -> list[RawListing]`, and register it in
`house_finder/sources/registry.py`. See `house_finder/sources/base.py`.

## Legal / etiquette

Scraping third-party sites may breach their Terms of Service. This tool is for
**personal, low-volume** use: it rate-limits requests, reuses a normal desktop
browser User-Agent, and honours `robots.txt` when it is available. You are
responsible for how you use it. Prefer official APIs/feeds where available,
and do not run it at high frequency or redistribute scraped data.
