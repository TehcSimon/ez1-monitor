# EZ1 Monitor

Schlanker Docker-Container zur Überwachung eines **APsystems EZ1-M** Wechselrichters
via lokaler API (Port 8050). Speichert alle Messwerte in SQLite und liefert ein
Web-Frontend mit Live-Daten, Tages-/Monats-/Jahresverlauf und kWh-Statistiken.

Kein Konto, keine Cloud, keine Telemetrie.

## Features

- Pollt den EZ1-M alle 60 s (konfigurierbar) via offizieller `apsystems-ez1` Lib
- Speichert PV1/PV2 Leistung, Tages- und Lebensdauerertrag, Online-Status
- Web-Dashboard mit:
  - Live-Leistung (gesamt + PV1/PV2 einzeln)
  - Auslastung im Verhältnis zum Drosselungslimit
  - Tagesverlauf-Chart (Leistung über Zeit)
  - Wochen-/Monats-/Jahres-Verlauf (Bar-Chart kWh)
  - Vergleichskarten: Heute vs. gestern, Woche, Monat
  - Spitzenleistung des Tages
  - Gesamterzeugung + CO₂- und Geldersparnis-Berechnung

## Voraussetzungen

1. Der EZ1-M hängt in deinem WLAN
2. Die **lokale API** ist aktiviert (in der AP EasyPower App unter den
   Wechselrichter-Einstellungen → "Local Mode" / "Lokale API")
3. Der WR hat eine bekannte IP (idealerweise im Router als Fix-IP/DHCP-Reservierung)

API-Endpunkt testen vor dem Start:
```
curl http://<EZ1-IP>:8050/getDeviceInfo
```
Sollte JSON mit `deviceId` etc. liefern.

## Quick Start (docker-compose)

```bash
git clone <repo>
cd ez1-monitor

# IP anpassen
nano docker-compose.yml

docker compose up -d
```

Dashboard öffnen: http://<host-ip>:8080

## Unraid-Setup (ohne Community-Template)

1. **In den Community Apps** ist das Image nicht enthalten – du baust es lokal.
2. Repository klonen:
   ```bash
   cd /mnt/user/appdata
   git clone <repo> ez1-monitor-src
   cd ez1-monitor-src
   docker build -t ez1-monitor:local .
   ```
3. **Docker → Container hinzufügen** in Unraid:
   - **Name:** `ez1-monitor`
   - **Repository:** `ez1-monitor:local`
   - **Network Type:** Bridge
   - **Port:** Host `8080` → Container `8080`
   - **Path:** Host `/mnt/user/appdata/ez1-monitor` → Container `/data`
   - **Variables:**
     - `INVERTER_IP` = `192.168.x.x` (deine EZ1-M IP)
     - `INVERTER_PORT` = `8050`
     - `POLL_INTERVAL` = `60`
     - `INSTALL_KWP` = `1.0`
4. **Apply** → Container startet, Dashboard unter `http://<unraid-ip>:8080`

## Konfiguration (Umgebungsvariablen)

| Variable | Default | Beschreibung |
|---|---|---|
| `INVERTER_IP` | `192.168.1.100` | IP-Adresse des EZ1-M im Heimnetz |
| `INVERTER_PORT` | `8050` | Port der lokalen API |
| `POLL_INTERVAL` | `60` | Sekunden zwischen API-Abfragen |
| `DB_PATH` | `/data/ez1.db` | Pfad zur SQLite-Datei |
| `INSTALL_KWP` | `1.0` | installierte Modul-Peakleistung in kWp |
| `LOG_LEVEL` | `INFO` | Python-Log-Level (DEBUG, INFO, WARNING) |

## Datenbank

SQLite-Datei unter `/data/ez1.db`. Bei 60-s-Polling ca. 1.440 Zeilen pro Tag,
≈ 500.000 pro Jahr. Bei ~80 Bytes pro Zeile = **~40 MB pro Jahr**. WAL-Modus ist
aktiv, also keine Locks im Normalbetrieb.

Backup: einfach die Datei `/mnt/user/appdata/ez1-monitor/ez1.db` mitkopieren
(Unraid macht das via Appdata-Backup-Plugin automatisch).

## API-Endpunkte (für eigene Skripte/Integrationen)

- `GET /api/live` — Letzte Messung + Gerät-Info + Config
- `GET /api/history?range=day|week|month|year` — Zeitreihen-Daten
- `GET /api/stats` — Zusammengefasste Statistiken inkl. Vergleichswerte

Alle liefern JSON. Beispiel:
```bash
curl http://<unraid-ip>:8080/api/live | jq
```

## Fehlersuche

**Status bleibt auf "offline" / "verbinde…"**
1. Container-Logs prüfen: `docker logs ez1-monitor`
2. Vom Container aus die API testen:
   ```bash
   docker exec ez1-monitor curl http://<EZ1-IP>:8050/getDeviceInfo
   ```
3. EZ1-M neu starten (Stecker für 30 s ziehen) und prüfen, ob die lokale API
   aktiv ist (AP EasyPower App → Settings)

**Datenbank zurücksetzen**
```bash
docker compose down
rm /mnt/user/appdata/ez1-monitor/ez1.db
docker compose up -d
```

## Lizenz

MIT — mach was du willst, garantiert ungewartet 🙃
