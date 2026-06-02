# EZ1 Monitor

[![Build and push container image](https://github.com/ThecSimon/ez1-monitor/actions/workflows/build-and-push.yml/badge.svg)](https://github.com/ThecSimon/ez1-monitor/actions/workflows/build-and-push.yml)

A lean, self-hosted monitoring dashboard for the **APsystems EZ1-M** microinverter.
Polls the inverter's local API, stores all measurements in SQLite, and serves
a web frontend with live data, historical charts, and lifetime statistics.

No cloud, no account, no telemetry. Localized for English and German.

<!-- TODO: add screenshot once dashboard is running with production data -->

## Features

- Polls the EZ1-M every 60 s (configurable) via the official `apsystems-ez1` library
- Stores PV1/PV2 power, today's energy, lifetime energy, online status
- Web dashboard with:
    - Live power (total + per-channel) and throttle utilization
    - Today's intraday curve
    - Week / Month / Year history (bar chart, kWh per day)
    - Period comparisons (today vs yesterday, week, month)
    - Lifetime totals: energy, CO₂ avoided, money saved
- UI in German or English (auto-detected from browser, can be forced via env)
- Configurable currency (EUR / USD) and electricity price
- Container healthcheck for clean integration

## Prerequisites

1. EZ1-M is on your network with a known IP (DHCP reservation recommended)
2. **Local API is enabled** on the inverter — in the AP EasyPower app under the
   inverter settings, set "Local Mode" to **Continuous**
3. The local API responds on port 8050:
   ```bash
   curl http://<EZ1-IP>:8050/getDeviceInfo
   ```
   This should return JSON with `deviceId`, `maxPower`, etc.

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
6. Add the environment variables listed below as needed
7. Apply — dashboard is at `http://<unraid-ip>:8080`

### Unraid Template Repository

For easier installation on Unraid, add this URL to **Apps → Settings → Template Repositories**:

## Configuration

All configuration is done via environment variables.

| Variable | Default | Description |
|---|---|---|
| `INVERTER_IP` | `192.168.1.194` | IP address of the EZ1-M inverter |
| `INVERTER_PORT` | `8050` | Port of the local API |
| `POLL_INTERVAL` | `60` | Seconds between API polls |
| `DB_PATH` | `/data/ez1.db` | Path to the SQLite database file |
| `INSTALL_KWP` | `1.0` | Installed peak power in kWp |
| `DEFAULT_LANG` | *(empty)* | `""` = auto-detect from browser, or force `de`/`en` |
| `CURRENCY` | `EUR` | `EUR` or `USD` — used for "money saved" display |
| `PRICE_PER_KWH` | `0.35` | Local electricity price per kWh |
| `CO2_KG_PER_KWH` | `0.38` | Grid CO₂ intensity (kg CO₂ per kWh) |
| `LOG_LEVEL` | `INFO` | Python log level (DEBUG, INFO, WARNING) |

## API Endpoints

For integrations and scripts:

| Endpoint | Description |
|---|---|
| `GET /health` | Container health check (returns `{"status":"ok"}`) |
| `GET /api/live` | Latest measurement + device info + runtime config |
| `GET /api/history?range=day\|week\|month\|year` | Historical data points |
| `GET /api/stats` | Aggregated statistics with period comparisons |

All endpoints return JSON. Example:

```bash
curl http://<host>:8080/api/live | jq
```

## Database

SQLite at `/data/ez1.db` (WAL mode). At 60 s polling intervals:
- ~1,440 rows per day
- ~525,000 rows per year
- ~40 MB per year of disk usage

Back up the file by copying it (Unraid appdata-backup plugin handles this automatically).

## Troubleshooting

**Status stays "connecting…" or "offline"**

1. Check container logs:
   ```bash
   docker logs ez1-monitor
   ```
2. Verify the inverter is reachable from inside the container:
   ```bash
   docker exec ez1-monitor curl http://<EZ1-IP>:8050/getDeviceInfo
   ```
3. Make sure the local API is set to **Continuous** in the AP EasyPower app — the
   default *Standard* mode disables it after 15 minutes of inactivity.

**Reset the database**

```bash
docker compose down
rm /mnt/user/appdata/ez1-monitor/ez1.db
docker compose up -d
```

## Development

Build locally:

```bash
git clone https://github.com/ThecSimon/ez1-monitor.git
cd ez1-monitor
docker build --platform linux/amd64 -t ez1-monitor:local .
docker run --rm -p 8080:8080 -e INVERTER_IP=<your-ip> ez1-monitor:local
```

The repo's GitHub Actions workflow builds multi-arch images (`linux/amd64`,
`linux/arm64`) and pushes them to GHCR on every push to `main`.

## Contributing

Pull requests welcome. Particularly appreciated:

- Additional UI translations (extend `app/static/i18n.js`)
- Bug reports with logs from `docker logs ez1-monitor`

## License

MIT — see [LICENSE](LICENSE).

Built for personal use with the APsystems EZ1-M. Not affiliated with APsystems.