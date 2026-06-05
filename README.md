# EZ1 Monitor

[![Build and push container image](https://github.com/ThecSimon/ez1-monitor/actions/workflows/build-and-push.yml/badge.svg)](https://github.com/ThecSimon/ez1-monitor/actions/workflows/build-and-push.yml)

A lean, self-hosted monitoring dashboard for the **APsystems EZ1-M** microinverter.
Polls the inverter's local API, stores all measurements in SQLite, and serves
a web frontend with live data, historical charts, stichtag and year-over-year
comparisons, plus lifetime statistics.

No cloud, no account, no telemetry. Localized for English and German.

<!-- TODO: add screenshot once dashboard is running with production data -->

## Features

- Polls the EZ1-M every 60 s (configurable) via the official `apsystems-ez1` library
- Stores PV1/PV2 power, today's energy, lifetime energy, online status
- Web dashboard with:
  - Live power (total + per-channel) with peak-of-day in the hero
  - Today's intraday curve
  - Week / Month / Year history (bar chart, kWh per day or per month)
  - **Stichtag comparisons** on all four cards: each compares today vs the
    equivalent moment in the previous period (yesterday until now, last week
    until same weekday, last month until same day-of-month, last year YTD)
    plus the full previous period as an anchor reference
  - **Year-over-year** on the month card (same calendar window vs last year
    plus full last-year-month as anchor)
  - Year history with **toggleable daily/monthly granularity**, dashed year
    boundary line, and dimmed previous-year bars
  - **Multi-year history** view (v1.4.0) — browse every year you've collected
    data for, with monthly or yearly granularity, served from a separate
    aggregate table that **survives the retention pruning** of the raw data
  - **Day picker** for browsing any past day in the Today's Curve chart
    (arrow keys, calendar picker, or drill-down from history)
  - **Light, dark, and system themes** with one-click toggle
  - Lifetime totals: energy, CO₂ avoided, money saved
- **Live grid CO₂ intensity** (v1.4.0, optional) via Electricity Maps API —
  every measurement is stamped with the grid factor that was active at that
  moment, so the lifetime CO₂ value is historically accurate instead of
  using a single static guess. Falls back through last-known-value → rolling
  average → static when the API is unavailable.
- **Long-term aggregates** — monthly and yearly totals are stored separately
  from the detail measurements, so cross-year comparisons survive even after
  the raw data has been pruned by retention
- **Prometheus `/metrics` endpoint** for Grafana, Home Assistant, or any
  other monitoring stack
- **Smart status indicator** with dusk/standby detection (no false alarms at night)
- **Adaptive polling** — slows down 10× when the inverter is offline
- **UID-agnostic non-root container** — runs on Docker, Kubernetes, and OpenShift
  with any UID assignment
- **Lean Alpine image** (~140 MB) with multi-stage build
- **iOS / Android home-screen support** (v1.4.0) — proper Apple touch icons
  plus a Web App Manifest, so "Add to Home Screen" gives the sun-with-shades
  branding instead of a generic letter
- UI in German or English (auto-detected from browser, can be forced via env)
- Configurable currency (EUR / USD) and electricity price
- Configurable data retention (default 2 years for year-over-year comparisons)
- Container healthcheck for clean integration

## Prerequisites

1. EZ1-M is on your network with a known IP (DHCP reservation recommended)
2. **Local API is enabled** on the inverter — in the AP EasyPower app under
   the inverter settings, set "Local Mode" to **Continuous**
3. The local API responds on port 8050:
   ```bash
   curl http://<EZ1-IP>:8050/getDeviceInfo
   ```

## Quick Start

### Option A: `docker run`

```bash
docker run -d \
  --name ez1-monitor \
  --restart unless-stopped \
  -p 8080:8080 \
  -v ez1-data:/data \
  -e INVERTER_IP=<EZ1-IP> \
  ghcr.io/thecsimon/ez1-monitor:latest
```

`INVERTER_IP` is **required** — the container fails to start without it.

### Option B: `docker compose`

Edit `docker-compose.yml` (set `INVERTER_IP` and your local values), then:

```bash
docker compose up -d
```

Open the dashboard at `http://<host>:8080`.

### Option C: Unraid

1. Docker tab → **Add Container**
2. Repository: `ghcr.io/thecsimon/ez1-monitor:latest`
3. Network Type: Bridge
4. Port: Host `8080` → Container `8080`
5. Path: Host `/mnt/user/appdata/ez1-monitor` → Container `/data`
6. Set `INVERTER_IP` to your EZ1-M's IP address (required)
7. Apply — dashboard is at `http://<unraid-ip>:8080`

### Option D: Kubernetes

The container is UID-agnostic and works with any `securityContext`. Example pod
spec snippet:

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000      # or any other UID
    fsGroup: 0           # required for /data volume write access
  containers:
    - name: ez1-monitor
      image: ghcr.io/thecsimon/ez1-monitor:latest
      env:
        - name: INVERTER_IP
          value: "192.168.1.100"
      volumeMounts:
        - name: data
          mountPath: /data
      ports:
        - containerPort: 8080
      livenessProbe:
        httpGet:
          path: /health
          port: 8080
```

On OpenShift, the container works out of the box with the default restricted SCC.

### Unraid Template Repository

For an easier install on Unraid, add this URL to **Apps → Settings →
Template Repositories**:

```
https://github.com/ThecSimon/ez1-monitor
```

## Upgrading from v1.0.x to v1.1.0

Version 1.1.0 changed the container user from root to UID 1000 (in group 0).
On most platforms this is transparent, but if you have an existing
`/data` directory from v1.0.x, you'll need to fix permissions once:

```bash
# Docker / Linux VM:
sudo chown -R 1000:0 /path/to/ez1-data
sudo chmod -R g=u /path/to/ez1-data

# Unraid:
sudo chown -R 1000:0 /mnt/user/appdata/ez1-monitor
sudo chmod -R g=u   /mnt/user/appdata/ez1-monitor
```

After this one-time fix, future updates will work transparently.

## Upgrading from v1.3.x to v1.4.0

No manual steps required. On first start the container:

- Adds three columns via `ALTER TABLE` to the existing database
  (`measurements.co2_g_per_kwh`, `monthly_aggregates.avg_co2_g_per_kwh`,
  `yearly_aggregates.avg_co2_g_per_kwh`). All migrations are idempotent and
  safe to re-run.
- Backfills the new aggregate average columns from existing measurement
  rows. The CO₂ averages will be `NULL` for any rows that pre-date the
  Electricity Maps integration — that's fine, the lifetime CO₂ calculation
  uses these values where available and falls back to the static factor
  otherwise.

To enable the optional Electricity Maps live integration, see the
[Live grid CO₂ intensity](#live-grid-co2-intensity-electricity-maps-integration)
section below.

## Configuration

All configuration is done via environment variables.

| Variable | Default | Description |
|---|---|---|
| `INVERTER_IP` | **required** | IP address or hostname of the EZ1-M inverter |
| `INVERTER_PORT` | `8050` | Port of the local API |
| `POLL_INTERVAL` | `60` | Seconds between API polls (auto-slowed 10× when offline) |
| `DB_PATH` | `/data/ez1.db` | Path to the SQLite database file |
| `INSTALL_KWP` | `1.0` | Installed peak power in kWp |
| `DEFAULT_LANG` | *(empty)* | `""` = auto-detect from browser, or force `de`/`en` |
| `CURRENCY` | `EUR` | `EUR` or `USD` |
| `PRICE_PER_KWH` | `0.35` | Local electricity price per kWh |
| `CO2_KG_PER_KWH` | `0.38` | Grid CO₂ intensity (kg CO₂ per kWh). Used as fallback when the Electricity Maps integration is off or unavailable. |
| `ELECTRICITY_MAPS_TOKEN` | *(empty)* | Optional. When set, the container fetches live grid CO₂ intensity once per hour. See the [Live grid CO₂ intensity](#live-grid-co2-intensity-electricity-maps-integration) section for setup. |
| `ELECTRICITY_MAPS_ZONE` | `DE` | ISO country code shown as the zone label in the UI. The actual zone is bound to the token in the Electricity Maps portal. |
| `TZ` | `Etc/UTC` | IANA timezone identifier (e.g. `Europe/Berlin`) |
| `RETENTION_DAYS` | `730` | Days to keep raw measurements. 0 = disable pruning. |
| `LOG_LEVEL` | `INFO` | Python log level (DEBUG, INFO, WARNING) |

> **Security note:** EZ1 Monitor has no built-in authentication. Run it on a
> trusted local network only, or place it behind a reverse proxy with auth
> before exposing it to the internet.

## Status indicator

The dot in the top-right tells you what the inverter is doing:

| Indicator | Meaning |
|---|---|
| 🟢 **online** (pulsing) | Inverter responds, everything fine |
| ⚪ **standby** (dim) | Inverter offline, but production was already winding down (dusk, night, snow). No alarm. |
| 🔴 **error** (pulsing) | Inverter offline mid-production — real alarm |
| ⚪ **no data** | Container just started, no successful poll yet |

The dashboard distinguishes standby from error by looking at the average power
over the last 5 minutes: below ~5 W is treated as a graceful wind-down,
above is treated as a problem worth attention.

## Stichtag comparisons

All four comparison cards (Today / Week / Month / Year) show a fair
"same-progress" comparison plus a "full period" anchor:

- **Heute** vs **gestern bis jetzt** (% delta) + **gestern gesamt** (anchor)
- **Diese Woche** vs **letzte Woche bis jetzt** (% delta) + **letzte Woche gesamt** (anchor)
- **Dieser Monat** vs **letzter Monat bis Stichtag** (% delta) + **letzter Monat gesamt** (anchor)
- **Dieses Jahr** vs **Vorjahr bis heute** (% delta)

The month card additionally shows year-over-year (same month last year), with
both the same calendar window and the full last-year month total.

## Day picker for the Today chart

The "Today's Curve" chart can browse any day within the data retention
window (default 2 years). Three ways to navigate:

- **Arrow buttons** `‹` and `›` jump one day backward / forward
- **Click the date label** to open a calendar picker for arbitrary jumps
- **Click any bar in the History chart** (Daily granularity) to drill down
  into that day's intraday curve

When viewing a historical day, live polling pauses for the Today chart
(historical data doesn't change anyway). The **"Today"** button next to
the picker returns you to live mode.

## Info tooltips on comparison cards

Each comparison card (Today / Week / Month / Year) has a small ⓘ icon next
to its title. Hover (or focus via keyboard) to see a brief explanation of
how the comparison is calculated. This is especially useful for understanding
the "short months effect" on the month card: when the previous month was
shorter than the current one (e.g. February → March), the comparison window
is clamped to the last day of the shorter month.

## Year history granularity

The history chart's "Jahr"/"Year" range has a toggle between two views:

- **Daily**: rolling last 365 days, one bar per day. Bars from the previous
  calendar year are dimmed, with a dashed vertical line marking 1 January
  of the current year.
- **Monthly**: rolling last 12 months, one bar per month. Same dim/line logic
  for year boundaries.

The toggle appears only when "Year" is the active range.

## Long-term aggregates

Detail measurements are pruned after `RETENTION_DAYS` (default 730 = 2 years),
but their summaries are kept indefinitely in two separate tables:

- `monthly_aggregates`: total kWh, peak W, days with data, energy-weighted
  avg CO₂ factor — per (year, month)
- `yearly_aggregates`: same fields rolled up per year

These are populated automatically on every container start (backfill from
existing measurements), then kept current by a background task that refreshes
the current month's aggregate every hour.

Read via:

```
GET /api/aggregates              -> { "yearly": [...] }
GET /api/aggregates?year=2026    -> { "year": 2026, "monthly": [...] }
```

The History chart's **Multi-year** tab pulls from these tables so it shows
every year you've collected data for, all the way back to the inverter's
first reading, even when those raw measurements have long since been pruned.

## Live grid CO₂ intensity (Electricity Maps integration)

By default the CO₂-avoided counter uses a static factor (`CO2_KG_PER_KWH`,
default 0.38 kg/kWh for Germany). That's a rough yearly average and tends
to overstate emissions because solar panels produce energy precisely when
the grid is cleanest (sunny, wind-rich daylight hours).

With the optional Electricity Maps integration the container fetches the
**live grid carbon intensity** for your zone once per hour and **stamps each
inverter measurement** with the factor that was active at that moment. The
lifetime CO₂ calculation is then a sum over all stamped measurements, which
gives Solar production its correctly low-CO₂ profile automatically — no
heuristics, just real grid data.

### Setup

1. Go to https://www.electricitymaps.com/free-tier-api and sign up.
2. During onboarding, choose the **Home Assistant** path. This is the free
   tier that gives permanent (non-trial) API access. Despite the name, the
   API key works with any HTTP client — Home Assistant is just how
   Electricity Maps markets the free tier.
3. Select your grid zone (e.g. `DE` for Germany). The zone is **locked for
   30 days** after first selection, so pick correctly.
4. Copy the API key shown on the Developer Hub page.
5. Set the env var on your container:

   ```
   ELECTRICITY_MAPS_TOKEN=your_api_key_here
   ELECTRICITY_MAPS_ZONE=DE
   ```

   The zone env var is for the UI display label only — the actual zone is
   bound to your token server-side.

### Fallback cascade

When the API returns useful data, the value is fresh and the source is
labeled `live`. If the API becomes unavailable, the resolver walks down a
three-tier cascade so the CO₂ display stays sensible:

| Time since last successful poll | Source     | UI label                                 |
|---------------------------------|------------|------------------------------------------|
| 0 – 6 hours                     | `live`     | "Live (DE) · 117 g/kWh · 22:00"          |
| 6 – 48 hours                    | `stale`    | "Last value (DE) · 117 g/kWh · 12 h ago" |
| > 48 hours                      | `avg`      | "Average (DE) · 248 g/kWh · over N polls" |
| no token / no data ever         | `static`   | "Static · 380 g/kWh"                     |

The rolling average is built from every successful poll since container
start, so it naturally reflects the mix of times the API was up.

### Endpoint used

Free Home-Assistant tier endpoint (no `zone` parameter — zone is bound to
the token in the portal):

```
GET https://api.electricitymap.org/v3/home-assistant
    -H "auth-token: <TOKEN>"
```

Returns `data.carbonIntensity` in gCO₂eq/kWh plus `data.fossilFuelPercentage`
and the `countryCode` of the zone. Rate-limited to 50 req/hour, the
container polls once per hour so you have 50× headroom.

## Prometheus metrics

The container exposes Prometheus-format metrics at `/metrics` for integration
with Grafana, Home Assistant, VictoriaMetrics, etc.

```
ez1_current_power_watts
ez1_pv1_power_watts, ez1_pv2_power_watts
ez1_today_kwh, ez1_pv1_today_kwh, ez1_pv2_today_kwh
ez1_this_week_kwh, ez1_this_month_kwh, ez1_this_year_kwh
ez1_peak_today_watts
ez1_lifetime_kwh_total
ez1_co2_saved_kg_total
ez1_status{state="online|standby|error|noData"}
ez1_info{device_id="...", serial_number="...", version="..."}
```

The endpoint has no authentication. Since the container is intended to run
in a LAN-only context, this is fine; if you want to expose it publicly, put
it behind your reverse proxy with auth.

## Themes

The dashboard ships with three theme modes accessible via the toggle button
in the top bar:

- **System** (default): follows the OS color scheme via `prefers-color-scheme`
- **Light**: warm off-white background with darker amber accent
- **Dark**: deep brown background with bright amber accent

The choice persists in `localStorage`. Switching between system/light/dark
re-renders the charts with the new theme colors.

## API Endpoints

For integrations and scripts:

| Endpoint | Description |
|---|---|
| `GET /health` | Container health check |
| `GET /api/live` | Latest measurement + device info + status + runtime config |
| `GET /api/history?range=day\|week\|month\|year` | Historical data points |
| `GET /api/history?range=day&date=YYYY-MM-DD` | Specific day's intraday curve |
| `GET /api/history?range=year&granularity=monthly` | Year view aggregated by month |
| `GET /api/history?range=multiyear&granularity=monthly` | All years, monthly bars |
| `GET /api/history?range=multiyear&granularity=yearly` | All years, one bar per year |
| `GET /api/stats` | Aggregated statistics with stichtag and YoY comparisons |
| `GET /api/aggregates` | Long-term yearly aggregates (survives retention) |
| `GET /api/aggregates?year=YYYY` | Monthly aggregates for a specific year |
| `GET /metrics` | Prometheus-format metrics |

All endpoints return JSON.

## Database

SQLite at `/data/ez1.db` (WAL mode). With normal day/night cycles and 60 s
polling, expect ~20 MB per year of disk usage.

Backups: copy the file while the container is running (WAL mode is safe for
read-while-write); the Unraid appdata-backup plugin handles this automatically.

## Troubleshooting

**Container won't start, exits immediately** — Check logs (`docker logs ez1-monitor`).
The most common cause is a missing or malformed `INVERTER_IP`.

**Permission denied writing to /data after upgrading to v1.1.0** —
See "Upgrading from v1.0.x to v1.1.0" above; permissions need a one-time fix.

**Status stays "no data" or "error"** —
1. Verify the inverter is reachable: `docker exec ez1-monitor curl http://<EZ1-IP>:8050/getDeviceInfo`
2. Make sure the local API is set to **Continuous** in the AP EasyPower app

**Reset the database** —
```bash
docker compose down
rm /mnt/user/appdata/ez1-monitor/ez1.db
docker compose up -d
```

## Development

```bash
git clone https://github.com/ThecSimon/ez1-monitor.git
cd ez1-monitor
docker build --platform linux/amd64 -t ez1-monitor:local .
docker run --rm -p 8080:8080 -e INVERTER_IP=<your-ip> ez1-monitor:local
```

The GitHub Actions workflow builds multi-arch images (`linux/amd64`,
`linux/arm64`) and pushes them to GHCR on every push to `main`.

## Contributing

Pull requests welcome. Particularly appreciated:

- Additional UI translations (extend `app/static/i18n.js`)
- Bug reports with logs from `docker logs ez1-monitor`

## License

MIT — see [LICENSE](LICENSE).

Built for personal use with the APsystems EZ1-M. Not affiliated with APsystems.
