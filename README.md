# EZ1 Monitor

[![Build and push container image](https://github.com/ThecSimon/ez1-monitor/actions/workflows/build-and-push.yml/badge.svg)](https://github.com/ThecSimon/ez1-monitor/actions/workflows/build-and-push.yml)

A lean, self-hosted monitoring dashboard for the **APsystems EZ1-M** microinverter.
Polls the local API, stores all measurements in SQLite, and serves a web UI
with live data, historical charts, same-period and year-over-year comparisons,
and lifetime statistics.

No cloud, no account, no telemetry. Localized for English and German.

<!-- TODO: add screenshot once dashboard is running with production data -->

## Features

- Polls the EZ1-M every 60 s via the official `apsystems-ez1` library
- Persists PV1/PV2 power, today's energy, lifetime energy, online status
- **Live dashboard**: hero card with total + per-channel power, today's
  peak (with timestamp), and average power during today's production window
- **Today's intraday curve** with a day picker for browsing any past day
  within the retention window (arrow navigation, calendar icon, or
  drill-down from the history chart)
- **Same-period (calendar-aligned) comparisons** on all four time-range cards
  (Today / Week / Month / Year), plus year-over-year on the month card
- **Best-day-per-period highlights** on the Week / Month / Year cards
  (best day this week, best day last week, etc.)
- **Hall of Fame**: all-time best day / week / month / year with subtle
  amber glow when records are fresh. Tier-unlocked so a new install
  doesn't blink permanently while it accumulates comparison data
- **History chart**: Week / Month / Year (with daily↔monthly toggle) and
  Multi-year — the latter is served from long-term aggregate tables that
  **survive retention pruning**
- **Live grid CO₂ intensity** (optional) via Electricity Maps — each
  measurement is stamped with the grid factor active at the time, so the
  lifetime CO₂ value is historically accurate. Three-tier fallback
  cascade (live → stale → rolling avg → static) keeps the display sensible
  if the API is unavailable
- **Smart status indicator** with dusk/standby detection (no false alarms
  at night) and adaptive polling (slows down 10× when offline)
- **Prometheus `/metrics`** endpoint for Grafana, Home Assistant, etc.
- **Themes**: system / light / dark, persisted in `localStorage`
- **PWA support**: installable on iOS and Android with branded icons
- **UID-agnostic non-root container** — runs on Docker, Kubernetes, and
  OpenShift with any UID

## Prerequisites

1. EZ1-M is on your network with a known IP (DHCP reservation recommended)
2. **Local API is enabled** on the inverter — in the AP EasyPower app under
   the inverter settings, set "Local Mode" to **Continuous**
3. The local API responds:
   ```bash
   curl http://<EZ1-IP>:8050/getDeviceInfo
   ```

## Quick Start

### Docker

```bash
docker run -d \
  --name ez1-monitor \
  --restart unless-stopped \
  -p 8080:8080 \
  -v ez1-data:/data \
  -e INVERTER_IP=<EZ1-IP> \
  ghcr.io/thecsimon/ez1-monitor:latest
```

`INVERTER_IP` is **required** — the container exits at startup without it.
Open the dashboard at `http://<host>:8080`.

### Docker Compose

Edit `docker-compose.yml` (set `INVERTER_IP` and local values), then:

```bash
docker compose up -d
```

### Unraid

1. Docker tab → **Add Container**
2. Repository: `ghcr.io/thecsimon/ez1-monitor:latest`
3. Network Type: Bridge, Port `8080:8080`
4. Path: `/mnt/user/appdata/ez1-monitor` → `/data`
5. Set `INVERTER_IP` to your EZ1-M's IP
6. Apply

For one-click install, add this URL under **Apps → Settings → Template
Repositories**: `https://github.com/ThecSimon/ez1-monitor`

### Kubernetes

The image is UID-agnostic. Minimal pod snippet:

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    fsGroup: 0           # required for /data volume write access
  containers:
    - name: ez1-monitor
      image: ghcr.io/thecsimon/ez1-monitor:latest
      env:
        - { name: INVERTER_IP, value: "192.168.1.100" }
      volumeMounts:
        - { name: data, mountPath: /data }
      ports:
        - { containerPort: 8080 }
      livenessProbe:
        httpGet: { path: /health, port: 8080 }
```

Works out of the box on OpenShift with the default restricted SCC.

## Configuration

All configuration via environment variables.

| Variable | Default | Description |
|---|---|---|
| `INVERTER_IP` | **required** | IP address or hostname of the EZ1-M |
| `INVERTER_PORT` | `8050` | Local API port |
| `POLL_INTERVAL` | `60` | Seconds between polls (auto-slowed 10× when offline) |
| `DB_PATH` | `/data/ez1.db` | SQLite database file |
| `INSTALL_KWP` | `1.0` | Installed peak power in kWp |
| `DEFAULT_LANG` | *(empty)* | `""` = auto-detect from browser, or force `de`/`en` |
| `CURRENCY` | `EUR` | `EUR` or `USD` |
| `PRICE_PER_KWH` | `0.35` | Local electricity price per kWh. Stamped on every measurement, so historical "money saved" stays accurate across tariff changes — update the value when your tariff changes and only new production is valued at the new price. |
| `CO2_KG_PER_KWH` | `0.38` | Static grid CO₂ factor (fallback when Electricity Maps is off or unavailable) |
| `ELECTRICITY_MAPS_TOKEN` | *(empty)* | Optional. Enables live grid CO₂ intensity. |
| `ELECTRICITY_MAPS_ZONE` | `DE` | ISO country code shown as zone label in the UI (the actual zone is bound to the token in the portal) |
| `TZ` | `Etc/UTC` | IANA timezone (e.g. `Europe/Berlin`) |
| `RETENTION_DAYS` | `730` | Days to keep raw measurements. 0 = disable pruning. |
| `LOG_LEVEL` | `INFO` | Python log level (DEBUG, INFO, WARNING) |

> **Security note:** No built-in authentication. Run on a trusted LAN only,
> or put it behind a reverse proxy with auth before exposing it publicly.

## Status indicator

The dot in the top-right shows what the inverter is doing:

| Indicator | Meaning |
|---|---|
| 🟢 **online** (pulsing) | Inverter responds, everything fine |
| ⚪ **standby** (dim) | Offline, but production was already winding down (dusk, night, snow). No alarm. |
| 🔴 **error** (pulsing) | Offline mid-production — real alarm |
| ⚪ **no data** | Container just started, no successful poll yet |

Standby vs error is decided by the 5-minute rolling average power: below
~5 W is treated as a graceful wind-down, above is treated as a problem.

## Live grid CO₂ intensity (optional)

By default the CO₂-avoided counter uses the static `CO2_KG_PER_KWH` factor.
That's a yearly average and tends to overstate emissions because solar
panels produce precisely when the grid is cleanest (sunny daylight hours).

With the Electricity Maps integration enabled, the container fetches the
**live grid carbon intensity** for your zone hourly and **stamps each
measurement** with the factor that was active at the time. The lifetime CO₂
value is then a sum over all stamped measurements, which automatically
gives solar production its correctly low-CO₂ profile.

### Setup

1. Sign up at <https://www.electricitymaps.com/free-tier-api>.
2. Choose the **Home Assistant** path — that's the free tier with permanent
   API access. (Despite the name, the key works with any HTTP client.)
3. Select your grid zone (e.g. `DE`). The zone is **locked for 30 days**
   after first selection, so pick correctly.
4. Set the env vars on your container:
   ```
   ELECTRICITY_MAPS_TOKEN=your_api_key_here
   ELECTRICITY_MAPS_ZONE=DE
   ```
   `ELECTRICITY_MAPS_ZONE` is for the UI label only — the actual zone is
   bound to your token server-side.

### Fallback cascade

| Time since last successful poll | Source   | UI label                                  |
|---------------------------------|----------|-------------------------------------------|
| 0 – 6 h                         | `live`   | "Live (DE) · 117 g/kWh · 22:00"           |
| 6 – 48 h                        | `stale`  | "Last value (DE) · 117 g/kWh · 12 h ago"  |
| > 48 h                          | `avg`    | "Average (DE) · 248 g/kWh · over N polls" |
| no token / no data ever         | `static` | "Static · 380 g/kWh"                      |

The rolling average is built from every successful poll since container
start, so it naturally reflects when the API was reachable.

### Endpoint used

Free Home-Assistant tier endpoint (no `zone` parameter — zone is server-side):

```
GET https://api.electricitymap.org/v3/home-assistant
    -H "auth-token: <TOKEN>"
```

Returns `data.carbonIntensity` in gCO₂eq/kWh plus `fossilFuelPercentage`
and `countryCode`. Rate-limited to 50 req/h; the container polls hourly,
so there's 50× headroom.

## Long-term aggregates

Raw measurements are pruned after `RETENTION_DAYS` (default 730 = 2 years),
but their summaries are kept indefinitely in two tables:

- `monthly_aggregates`: total kWh, peak W, days with data, energy-weighted
  avg CO₂ factor and avg electricity price — per (year, month)
- `yearly_aggregates`: same fields rolled up per year

These are populated on container start (backfill from existing measurements)
and kept current by a background task that refreshes the current month's
aggregate hourly. The History chart's **Multi-year** view reads from them,
so it shows every year you've collected data for — even after the raw rows
have been pruned.

## Themes

Three theme modes via the toggle in the top bar:

- **System** (default): follows the OS color scheme via `prefers-color-scheme`
- **Light**: cool slate-white background with electric-blue accent
- **Dark**: deep brown background with warm amber accent

The choice persists in `localStorage`. Switching re-renders the charts
with the active theme colors.

## API Endpoints

For integrations and scripts:

| Endpoint | Description |
|---|---|
| `GET /health` | Container health check |
| `GET /api/live` | Latest measurement + device info + status + runtime config |
| `GET /api/history?range=day\|week\|month\|year` | Historical data points |
| `GET /api/history?range=day&date=YYYY-MM-DD` | Specific day's intraday curve |
| `GET /api/history?range=year&granularity=monthly` | Year view aggregated by month |
| `GET /api/history?range=multiyear&granularity=monthly\|yearly` | All years |
| `GET /api/stats` | Aggregated statistics with same-period and YoY comparisons |
| `GET /api/highscores` | All-time best day/week/month/year with animation state |
| `GET /api/aggregates` | Long-term yearly aggregates (survives retention) |
| `GET /api/aggregates?year=YYYY` | Monthly aggregates for a specific year |
| `GET /metrics` | Prometheus-format metrics |

All endpoints return JSON. `/metrics` returns Prometheus exposition format.

### Prometheus metrics

```
ez1_current_power_watts
ez1_pv1_power_watts, ez1_pv2_power_watts
ez1_today_kwh, ez1_pv1_today_kwh, ez1_pv2_today_kwh
ez1_this_week_kwh, ez1_this_month_kwh, ez1_this_year_kwh
ez1_peak_today_watts
ez1_lifetime_kwh_total
ez1_co2_saved_kg_total
ez1_carbon_intensity_g_per_kwh
ez1_carbon_fossil_percentage
ez1_carbon_source{source="live|stale|avg|static"}
ez1_status{state="online|standby|error|noData"}
ez1_info{device_id="...", firmware="...", version="..."}
```

No authentication. LAN-only by design — put it behind your reverse proxy
with auth if you need to expose it publicly.

## Database

SQLite at `/data/ez1.db` in WAL mode. With normal day/night cycles and
60 s polling, expect ~20 MB per year on disk.

Backups: copy the file while the container is running (WAL is safe for
read-while-write). The Unraid appdata-backup plugin handles this
automatically.

## Upgrading

### From v1.6.2 to v1.6.3

No manual steps, no database migration — a small bug-fix and polish release.

- **Per-panel "today" production survives inverter standby.** The PV 1 / PV 2
  cards read today's kWh from the live inverter reading, which is null once
  the inverter drops to standby at night, so they showed "—" overnight while
  peak and average (both DB-derived) stayed correct. The per-panel day total
  is now read from the database (`MAX(e1)`/`MAX(e2)` for today) like the
  other figures. No schema change — `e1`/`e2` were already stored per row.
- **Shorter record-glow durations.** The Hall-of-Fame highlight now fades
  sooner: day record glows for the set day + 1 (was + 2), week + 2 (was + 3),
  month + 3 (was + 5). Year is unchanged (+ 7).
- **Device header is now stacked and labeled.** Serial number, max output and
  firmware were a single dense line under the title; they're now three labeled
  lines (`S/N`, `max. output`, `Firmware`). The max output continues to be
  read from the inverter, not hard-coded. On mobile the theme toggle and status
  pill move into a compact right-hand column (toggle on top, status below) so
  the multi-line header stays compact instead of stacking a third row.

### From v1.6.1 to v1.6.2

No manual steps, no database migration — a bug-fix and cleanup release.

- **Energy windows now bucket by local calendar day** instead of by UTC
  day. The four stat cards (today/week/month/year and their comparisons)
  were correct in timezones near UTC, but in far-east offsets (e.g.
  UTC+9/+10) the UTC-day boundary falls mid-morning local time, which
  could double-count the morning production ramp in the "today" figure.
- **The retention-boundary day stays frozen during daily-aggregate
  backfill.** The boundary day's early rows are pruned by the time
  backfill runs, so recomputing it could lower its stored peak. It is now
  left at its complete stored value (`>` instead of `>=` on the boundary).
- **Month-rollover gap closed.** The hourly aggregate refresh now
  finalizes the month that just ended exactly once at rollover, instead
  of waiting for the next container restart to pick up its final hour.
- **Frontend date parsing fixed for the Americas.** History-chart tooltips
  and the year-view month labels parsed `YYYY-MM-DD` as UTC midnight,
  rendering the previous local day at negative UTC offsets; they now parse
  at local midnight.
- **`PRICE_PER_KWH=0` / `RETENTION_DAYS=0` are honored by the UI.** A
  configured `0` no longer falls back to the default (nullish-coalescing
  instead of truthy fallback); with retention disabled the day picker now
  allows browsing the full history instead of clamping to today.
- Removed unused database methods, pinned the Chart.js date adapter to a
  fixed version, and moved the test suite to native async (`pytest-asyncio`)
  so CI runs on the same Python (3.14) the runtime image ships.
- **All front-end assets are now self-hosted** — Chart.js, the date-fns
  adapter, flatpickr and the web fonts (Inter, JetBrains Mono, Bricolage
  Grotesque, latin subset) ship inside the image under `app/static/vendor/`.
  The dashboard now renders fully offline with zero third-party requests,
  which also removes the Google Fonts hotlink (a documented GDPR issue in
  Germany, since it leaks the visitor IP to Google). Adds ~450 KB to the
  image.
- **Window-chrome color is now neutral.** The macOS "Add to Dock" web-app
  title bar (and Android PWA chrome) followed the amber accent
  `theme-color`; it now matches the dashboard background per system scheme
  (`#0c0a08` dark / `#f8fafc` light) so the window edge blends with the
  header instead of showing an amber band. UI accent colors are unchanged.

### From v1.6.0 to v1.6.1

No manual steps. On first start the container runs idempotent
`ALTER TABLE` migrations adding the electricity-price columns
(`price_per_kwh` on measurements, `avg_price_per_kwh` on the aggregate
tables) and a `firmware` column on `device_info`. Existing rows keep
`NULL` prices — the "money saved" calculation falls back to the current
`PRICE_PER_KWH` for that unstamped portion, exactly like the CO₂
calculation handles pre-v1.4 rows.

Notable fixes in this release:

- **Long-term aggregates are no longer overwritten with partial values**
  when the startup backfill runs after raw measurements at the retention
  boundary have been pruned. Months outside the retention window stay
  frozen at the value computed while their data was complete.
- **Daily aggregates (Hall of Fame) now group by local calendar date**
  instead of UTC — for timezones west of UTC, evening production was
  attributed to the wrong day.
- **Lifetime CO₂ and money values are now derived from the monthly
  aggregates** (plus the current month live), so they remain
  historically accurate after raw rows are pruned.
- Fixed a frontend interval leak that stacked up Hall-of-Fame refresh
  timers on every online/standby status change.
- Fixed the ambient background glow appearing as a "sticky shadow" when
  scrolling on iOS Safari in dark mode.
- The `ez1_info` Prometheus metric now exports the inverter firmware as
  `firmware` (previously mislabeled `serial_number`), and the dashboard
  shows the firmware version in the device subtitle.
- The container image is roughly 40 MB smaller (~110 MB instead of
  ~150 MB): plain `uvicorn` instead of `uvicorn[standard]` (uvloop,
  websockets, watchfiles and httptools were never used), busybox `wget`
  instead of `curl` for the healthcheck, and no pip in the runtime
  stage. No functional change.

### From v1.5.x to v1.6.x

No manual steps. On first start the container creates the new
`daily_aggregates` table and backfills it from existing measurements
(typically 1-3 seconds, up to ~15 seconds on a fully populated 2-year
install on a Raspberry Pi). Subsequent starts are instant. The new
table powers the Hall of Fame card and the per-period "best day"
references on the Week/Month/Year stat cards.

### From v1.4.x to v1.5.x

No manual steps and no database migration. v1.5 is an internal cleanup
release — backend query consolidation, language-detection caching, and
the first round of unit tests. Behavior, API, and UI are unchanged from
v1.4.3.

### From v1.3.x to v1.4.x

No manual steps. On first start the container runs three idempotent
`ALTER TABLE` migrations to add CO₂ columns, then backfills the new
aggregate average columns from existing measurements. Old rows keep
`NULL` CO₂ factors — the lifetime CO₂ calc handles that automatically by
falling back to the static factor for the unmeasured portion.

### From v1.0.x to v1.1.0+

v1.1.0 changed the container user from root to UID 1000 (group 0). On
most platforms this is transparent; existing `/data` volumes need a
one-time permission fix:

```bash
sudo chown -R 1000:0 /path/to/ez1-data
sudo chmod -R g=u   /path/to/ez1-data
```

## Troubleshooting

**Container won't start, exits immediately** — Check logs
(`docker logs ez1-monitor`). The most common cause is a missing or
malformed `INVERTER_IP`.

**Status stays "no data" or "error"** —
1. Verify the inverter is reachable: `curl http://<EZ1-IP>:8050/getDeviceInfo`
2. Confirm Local Mode is set to **Continuous** in the AP EasyPower app

**Reset the database** —
```bash
docker compose down
rm /path/to/ez1.db
docker compose up -d
```

## Development

```bash
git clone https://github.com/ThecSimon/ez1-monitor.git
cd ez1-monitor
docker build --platform linux/amd64 -t ez1-monitor:local .
docker run --rm -p 8080:8080 -e INVERTER_IP=<your-ip> ez1-monitor:local
```

The GitHub Actions workflow runs the test suite and builds multi-arch
images (`linux/amd64`, `linux/arm64`) on every push to `main`, pushing
the result to GHCR.

### Tests

Two suites: pure unit tests for the date-math helpers in
`app/date_helpers.py` (leap years, month-end boundaries, century-year
edge cases) and async integration tests in `tests/test_aggregates.py`
that spin up a temporary SQLite database and verify the aggregate and
Hall-of-Fame queries (including the retention-boundary freeze). Run them
locally with:

```bash
pip install -r requirements-dev.txt
pytest
```

## License

MIT — see [LICENSE](LICENSE).

Built for personal use with the APsystems EZ1-M. Not affiliated with APsystems.
