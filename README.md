# EZ1 Monitor

[![Build and push container image](https://github.com/TehcSimon/ez1-monitor/actions/workflows/build-and-push.yml/badge.svg)](https://github.com/TehcSimon/ez1-monitor/actions/workflows/build-and-push.yml)

A lean, self-hosted monitoring dashboard for the **APsystems EZ1-M** microinverter.
Polls the local API, stores all measurements in SQLite, and serves a web UI
with live data, historical charts, same-period and year-over-year comparisons,
and lifetime statistics.

No cloud, no account, no telemetry. Localized for English and German.

> **Project status: feature-complete (LTS).** v1.9.1 is the first
> long-term-support release. The project is actively maintained with bug
> fixes and dependency updates, but no new features are planned.

<!-- TODO: add screenshot once dashboard is running with production data -->

## Features

- Polls the EZ1-M via the official `apsystems-ez1` library
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
  doesn't blink permanently while it accumulates comparison data. Click a
  tile to jump straight to that period in the history view
- **History chart**: Week / Month / Year (with daily / weekly / monthly
  toggle) and Multi-year. Click any weekly or monthly bar to drill into that
  exact period — daily bars plus a total / average / best-day / year-over-year
  summary — then into a single day's curve. Multi-year is served from
  long-term aggregate tables that **survive retention pruning**
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
  ghcr.io/tehcsimon/ez1-monitor:latest
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
2. Repository: `ghcr.io/tehcsimon/ez1-monitor:latest`
3. Network Type: Bridge, Port `8080:8080`
4. Path: `/mnt/user/appdata/ez1-monitor` → `/data`
5. Set `INVERTER_IP` to your EZ1-M's IP
6. Apply

For one-click install, add this URL under **Apps → Settings → Template
Repositories**: `https://github.com/TehcSimon/ez1-monitor`

### Kubernetes

The image is UID-agnostic. Minimal pod snippet:

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    fsGroup: 0           # required for /data volume write access
  containers:
    - name: ez1-monitor
      image: ghcr.io/tehcsimon/ez1-monitor:latest
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
| `CURRENCY` | `EUR` | ISO 4217 code used for money formatting (e.g. `EUR`, `USD`, `CHF`) |
| `PRICE_PER_KWH` | `0.35` | Local electricity price per kWh. Stamped on every measurement, so historical "money saved" stays accurate across tariff changes — update the value when your tariff changes and only new production is valued at the new price. |
| `SELF_CONSUMPTION_PCT` | `100` | Estimated share (%) of production you actually self-consume. Without a battery/smart control you can't use 100% — the rest is fed in. Affects only the realistic "money saved" and amortization; the kWh and CO₂ figures are unchanged. `100` reproduces the previous behaviour. |
| `FEED_IN_TARIFF` | `0` | Feed-in compensation per kWh for the share you don't self-consume (same currency as `PRICE_PER_KWH`). Commonly `0` for a balcony plant. |
| `INSTALL_COST` | `0` | One-off total cost of your installation. When set (> 0), a "Payback" card shows how far your savings have repaid it, with a break-even highlight. `0` hides the card. |
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
| 🟠 **connection error** | The dashboard can't reach its own backend (network blip, container restarting) — says nothing about the inverter |

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
| `GET /api/history?range=year&granularity=weekly` | Year view aggregated by ISO week |
| `GET /api/history?range=week&week=YYYY-Www` | A specific historical ISO week (daily bars + summary) |
| `GET /api/history?range=month&month=YYYY-MM` | A specific historical month (daily bars + summary) |
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
ez1_money_saved
ez1_amortization_percent
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

### From v1.9.0 to v1.9.1

No manual steps, no database migration. A bug-fix release — and the first
**LTS release**: EZ1 Monitor is feature-complete, future versions contain
bug fixes and dependency updates only.

- **The aggregate backfill no longer loses pre-retention history.** Months
  and days older than `RETENTION_DAYS` without a stored aggregate (imported
  databases, upgrades from pre-aggregate versions) were skipped at startup —
  and their raw rows then pruned for good ~60 s later. They are now
  aggregated from the remaining raw rows first; existing (frozen) aggregates
  stay untouched, exactly as before.
- **Year-view daily bars are now correct west of UTC.** The daily buckets of
  `/api/history?range=year` grouped by UTC day, so at negative UTC offsets
  each bar could show `max(day, next day)`. They now use the local calendar
  day like every other daily query.
- **Docs and templates pointed at a misspelled GitHub namespace**
  (`ThecSimon` → `TehcSimon`): badge, `docker pull` commands, clone URL and
  Unraid template links now match the real repository and GHCR image.
- Device info (serial, firmware, AC limit) is retried after every successful
  poll instead of only 10 attempts at startup — a container (re)started at
  night no longer shows placeholders until the next restart.
- `/metrics`: `ez1_pv1_today_kwh` / `ez1_pv2_today_kwh` are DB-derived like
  the dashboard's PV cards and roll over to 0 at local midnight instead of
  holding yesterday's counters overnight.
- Stat windows are half-open, so a measurement landing exactly on a window
  boundary (midnight) is no longer counted into two adjacent windows.
- A calm amber **"connection error"** pill when the dashboard can't reach
  its own backend, instead of the red inverter-error state.
- Electricity Maps polling retries with backoff (5 → 40 min) after a failed
  poll instead of silently waiting a full hour.
- Charts update in place instead of being rebuilt on every refresh (no more
  periodic flicker), and stale responses from rapid tab/day switches can no
  longer overwrite the newest view.
- Dropped a redundant SQLite index on `measurements` (the timestamp PRIMARY
  KEY already is the table's btree).

### From v1.8.x to v1.9.0

No manual steps, no database migration. A feature release — everything is
computed from the existing `daily_aggregates` table (which survives raw-row
pruning), so anchored views of old weeks/months work even after their raw
measurements are gone.

- **Weekly history view.** The Year range on the History card gains a third
  granularity, **Weekly** (≈52 ISO-week bars), alongside Daily and Monthly —
  the most readable density between 365 daily and 12 monthly bars. Rolling
  (last 52 weeks) and calendar (this year) both apply.
- **Drill into any week or month.** Click a weekly or monthly bar to open that
  exact period as daily bars, with a **summary line**: total, average per day,
  best day, and data-gated deltas vs. the previous period and vs. the same
  period last year. A "back" link returns to the overview; clicking a day
  drills further into its intraday curve.
- **Hall of Fame is now navigable.** Click the best-day / week / month tile to
  jump to that period (best-year opens the all-years overview). Tiles are
  keyboard-focusable buttons.
- **Harmonized stat cards.** The Week card gains a year-over-year line ("same
  ISO week last year") to match the Month card — both are now data-gated
  (hidden on young installs with nothing to compare). The Month card's "full
  month last year" line was removed; that figure is reachable via the new
  drill-down.

### From v1.7.0 to v1.8.0

No manual steps, no database migration. Feature release: realistic "money
saved" via `SELF_CONSUMPTION_PCT` + `FEED_IN_TARIFF` (defaults reproduce
the old behaviour), the amortization card via `INSTALL_COST` (with
break-even glow), a fixed/auto Y-axis toggle for the day chart, and two
new Prometheus metrics (`ez1_money_saved`, `ez1_amortization_percent`).
All three variables are described in the Configuration table above.

### From v1.6.3 to v1.7.0

No manual steps, no database migration. Feature release: rolling vs.
calendar mode for the Week / Month / Year charts (persisted toggle on the
History card), the device header now labels the inverter's output cap as
**AC limit**, plus day-picker icon and layout polish.

### From v1.6.2 to v1.6.3

No manual steps, no database migration. Bug-fix/polish release: per-panel
"today" kWh survives inverter standby (now DB-derived), shorter
Hall-of-Fame glow durations, a stacked labeled device header, and mobile
time-axis labels that no longer overlap.

### From v1.6.1 to v1.6.2

No manual steps, no database migration. Bug-fix/cleanup release: stat
windows bucket by local calendar day (fixes double-counting in far-east
timezones), the retention-boundary day stays frozen during backfill, the
month-rollover gap in the hourly refresh was closed, frontend date parsing
fixed for the Americas, and `PRICE_PER_KWH=0` / `RETENTION_DAYS=0` are
honored by the UI. All frontend assets (Chart.js, flatpickr, fonts) are
self-hosted since this release — the dashboard renders fully offline with
zero third-party requests (which also removed the Google-Fonts GDPR
issue).

### From v1.6.0 to v1.6.1

No manual steps. On first start the container runs idempotent `ALTER
TABLE` migrations (electricity-price columns, `firmware` on
`device_info`); existing rows keep `NULL` prices and fall back to the
current `PRICE_PER_KWH`, like the CO₂ handling of pre-v1.4 rows. Notable
fixes: long-term aggregates are no longer overwritten with partial values
at the retention boundary, daily aggregates group by local calendar date,
lifetime CO₂/money stay accurate after pruning, a Hall-of-Fame timer leak
and an iOS scroll artifact were fixed, `ez1_info` exports the firmware
under the correct label, and the image shrank by ~40 MB.

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
git clone https://github.com/TehcSimon/ez1-monitor.git
cd ez1-monitor
docker build --platform linux/amd64 -t ez1-monitor:local .
docker run --rm -p 8080:8080 -e INVERTER_IP=<your-ip> ez1-monitor:local
```

The GitHub Actions workflow runs the test suite and builds multi-arch
images (`linux/amd64`, `linux/arm64`) on every push to `main`, pushing
the result to GHCR.

### Tests

Four suites: pure unit tests for the date-math helpers
(`test_date_helpers.py`) and the money/amortization model
(`test_amortization.py`), plus async integration tests
(`test_aggregates.py`, `test_history.py`) that spin up a temporary SQLite
database and verify the aggregate, history and Hall-of-Fame queries —
including the retention-boundary freeze and the pre-retention backfill
regression guards. Run them locally with:

```bash
pip install -r requirements-dev.txt
pytest
```

## License

MIT — see [LICENSE](LICENSE).

Built for personal use with the APsystems EZ1-M. Not affiliated with APsystems.
