# EZ1 Monitor

[![Build and push container image](https://github.com/ThecSimon/ez1-monitor/actions/workflows/build-and-push.yml/badge.svg)](https://github.com/ThecSimon/ez1-monitor/actions/workflows/build-and-push.yml)

A lean, self-hosted monitoring dashboard for the **APsystems EZ1-M** microinverter.
Polls the inverter's local API, stores all measurements in SQLite, and serves
a web frontend with live data, historical charts, period-over-period and
year-over-year comparisons, plus lifetime statistics.

No cloud, no account, no telemetry. Localized for English and German.

<!-- TODO: add screenshot once dashboard is running with production data -->

## Features

- Polls the EZ1-M every 60 s (configurable) via the official `apsystems-ez1` library
- Stores PV1/PV2 power, today's energy, lifetime energy, online status
- Web dashboard with:
  - Live power (total + per-channel) with peak-of-day in the hero
  - Today's intraday curve
  - Week / Month / Year history (bar chart, kWh per day)
  - Period comparisons (today vs yesterday, week, month, year)
  - **Year-over-year comparison** on the month card (same month last year,
    same calendar window for a fair %-delta)
  - Lifetime totals: energy, CO₂ avoided, money saved
- **Smart status indicator** — distinguishes between "online", "standby"
  (e.g. dusk / nighttime), and real "error" conditions, no false alarms at night
- **Adaptive polling** — slows down 10× when the inverter is offline, keeping
  the database clean and the UI calm
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

### Unraid Template Repository

For an easier install on Unraid that pre-fills all fields, add this URL
to **Apps → Settings → Template Repositories**:

```
https://github.com/ThecSimon/ez1-monitor
```

After saving, search for "ez1-monitor" in the Apps tab.

## Configuration

All configuration is done via environment variables.

| Variable | Default | Description |
|---|---|---|
| `INVERTER_IP` | **required** | IP address or hostname of the EZ1-M inverter. The container refuses to start if this is empty or invalid. |
| `INVERTER_PORT` | `8050` | Port of the local API |
| `POLL_INTERVAL` | `60` | Seconds between API polls (slowed 10× automatically when the inverter is offline) |
| `DB_PATH` | `/data/ez1.db` | Path to the SQLite database file |
| `INSTALL_KWP` | `1.0` | Installed peak power in kWp |
| `DEFAULT_LANG` | *(empty)* | `""` = auto-detect from browser, or force `de`/`en` |
| `CURRENCY` | `EUR` | `EUR` or `USD` — used for "money saved" display |
| `PRICE_PER_KWH` | `0.35` | Local electricity price per kWh |
| `CO2_KG_PER_KWH` | `0.38` | Grid CO₂ intensity (kg CO₂ per kWh) |
| `TZ` | `Etc/UTC` | IANA timezone identifier (e.g. `Europe/Berlin`, `America/New_York`) |
| `RETENTION_DAYS` | `730` | Days to keep raw measurements. 0 = disable pruning. |
| `LOG_LEVEL` | `INFO` | Python log level (DEBUG, INFO, WARNING) |

> **Security note:** EZ1 Monitor has no built-in authentication. Run it on a
> trusted local network only, or place it behind a reverse proxy with auth
> (nginx-proxy-manager, Authelia, Caddy with basic-auth, …) before exposing
> it to the internet.

## Status indicator

The dot in the top-right tells you at a glance what the inverter is doing:

| Indicator | Meaning |
|---|---|
| 🟢 **online** (pulsing) | Inverter responds to polls, everything is fine |
| ⚪ **standby** (dim, no pulse) | Inverter is offline but production was already winding down (dusk, night, snow on panels). No alarm. |
| 🔴 **error** (pulsing) | Inverter is offline while it should be producing — real alarm (defect, network issue, …) |
| ⚪ **no data** | Container just started, no successful poll yet |

The dashboard distinguishes standby from error by looking at the average power
over the last 5 minutes before the inverter went silent: below ~5 W is treated
as a graceful wind-down, above is treated as a problem worth your attention.

## Year-over-year comparison

After running for a year, the month card automatically gains a year-over-year
section. It compares the **same calendar window** in both years for a fair
percentage delta, and also shows the full-month total of last year as an
anchor reference. With the default 2-year retention, you get the full
year-over-year comparison capability once the dashboard has been running
through one full annual cycle.

## API Endpoints

For integrations and scripts:

| Endpoint | Description |
|---|---|
| `GET /health` | Container health check |
| `GET /api/live` | Latest measurement + device info + status + runtime config |
| `GET /api/history?range=day\|week\|month\|year` | Historical data points |
| `GET /api/stats` | Aggregated statistics with period and year-over-year comparisons |

All endpoints return JSON.

## Database

SQLite at `/data/ez1.db` (WAL mode). With normal day/night cycles and 60 s
polling, expect ~20 MB per year of disk usage.

Backups: copy the file while the container is running (WAL mode is safe for
read-while-write); the Unraid appdata-backup plugin handles this automatically.

## Troubleshooting

**Container won't start, exits immediately**

Check the logs:
```bash
docker logs ez1-monitor
```
The most common cause is a missing or malformed `INVERTER_IP`.

**Status stays "no data" or "error"**

1. Verify the inverter is reachable from inside the container:
   ```bash
   docker exec ez1-monitor curl http://<EZ1-IP>:8050/getDeviceInfo
   ```
2. Make sure the local API is set to **Continuous** in the AP EasyPower app —
   the default *Standard* mode disables the local API after 15 min of
   inactivity, which looks indistinguishable from a hardware failure.

**Reset the database**

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

The GitHub Actions workflow builds multi-arch images
(`linux/amd64`, `linux/arm64`) and pushes them to GHCR on every push to `main`.

## Contributing

Pull requests welcome. Particularly appreciated:

- Additional UI translations (extend `app/static/i18n.js`)
- Bug reports with logs from `docker logs ez1-monitor`

## License

MIT — see [LICENSE](LICENSE).

Built for personal use with the APsystems EZ1-M. Not affiliated with APsystems.
