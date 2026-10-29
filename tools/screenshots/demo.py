"""Synthetic balcony plant for the README screenshots.

One deterministic model feeds both halves of a realistic demo setup:

    python demo.py build-db /data/ez1.db   # history → demo SQLite database
    python demo.py serve [port]            # live → fake EZ1-M local API

A real ez1-monitor container then runs against the pair unmodified: the
database supplies ~2.5 years of history, and the fake inverter answers the
poller, so the dashboard shows status "online" and today's curve keeps
growing. Every value is derived from the calendar date (seeded RNG), so the
database and the live API agree without sharing any state.

Stdlib only, so it runs in the ez1-monitor image itself. Needs TZ set to the
same zone as the monitor, and the same (pinned) clock — compose.yml provides
both. Not affiliated with any real device: the serial is obviously fake.
"""
import json
import math
import random
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- The demo installation ---------------------------------------------------

FIRST_DAY = date(2024, 4, 6)       # first day the monitor recorded
# Lifetime counters (kWh) the inverter had already accumulated before the
# monitor was set up — real installs rarely start both on the same day.
PRE_MONITOR_KWH = (11.4, 10.9)
AC_LIMIT_W = 800
DEVICE_ID = "E17DEMO00001"
FIRMWARE = "EZ1 1.7.0"
CO2_G_PER_KWH = 380.0              # static factor, as without Electricity Maps
TARIFF_CHANGE = date(2026, 1, 1)   # 0.32 → 0.35 per kWh, stamped per row
DENSE_DAYS = 62                    # recent days at the poller's 60 s cadence
SPARSE_STEP_MIN = 15               # older days: one row every 15 minutes


def _season(d: date) -> float:
    """+1 at the summer solstice, -1 at the winter solstice."""
    return math.cos(2 * math.pi * (d.timetuple().tm_yday - 172) / 365.25)


def _dst_hours(d: date) -> int:
    noon = datetime(d.year, d.month, d.day, 12).timestamp()
    return 1 if time.localtime(noon).tm_isdst > 0 else 0


def _shape(x: float) -> float:
    """Clear-sky bell over the daylight fraction x in [0, 1]."""
    return math.sin(math.pi * x) ** 1.3 if 0.0 < x < 1.0 else 0.0


@lru_cache(maxsize=None)
def day_profile(d: date) -> tuple:
    """Per-minute (p1, p2) watts of local wall-clock day d, plus the first
    and last minute the inverter is awake. Two panels facing slightly east
    and west of south, under that day's (seeded) weather."""
    rng = random.Random(d.toordinal() * 7919)
    s = _season(d)
    daylight_h = 12.2 + 4.3 * s
    noon_h = 12 + 20 / 60 + _dst_hours(d)      # solar noon at ~10° E
    rise_h, set_h = noon_h - daylight_h / 2, noon_h + daylight_h / 2
    peak_w = 500 + 130 * s                     # clear-sky DC peak, both inputs

    sunny_odds = 0.38 + 0.2 * s                # grey winters, sunnier summers
    roll = rng.random()
    # A sunny streak into "today" (the pinned demo date, see compose.yml)
    # gives the live curve, the week view and the record-pace pill
    # something to show.
    if 0 <= (date.today() - d).days < 4 or roll < sunny_odds:
        kind, level = "sunny", rng.uniform(0.9, 1.0)
    elif roll < sunny_odds + 0.33:
        kind, level = "mixed", rng.uniform(0.4, 0.75)
    else:
        kind, level = "overcast", rng.uniform(0.1, 0.3)

    minutes = []
    cloud = level
    for m in range(1440):
        h = m / 60
        if kind == "sunny":
            t = level * (1 + rng.gauss(0, 0.006))
        elif kind == "mixed":
            # Mean-reverting random walk: passing clouds, sunny spells.
            cloud += rng.gauss(0, 0.07) + 0.06 * (level - cloud)
            cloud = min(1.05, max(0.15, cloud))
            t = cloud
        else:
            t = level * (1 + rng.gauss(0, 0.03))
        x = (h - rise_h) / (set_h - rise_h)
        p1 = 0.51 * peak_w * _shape(x + 0.025) * t
        p2 = 0.49 * peak_w * _shape(x - 0.025) * t
        if p1 + p2 > AC_LIMIT_W:
            k = AC_LIMIT_W / (p1 + p2)
            p1, p2 = p1 * k, p2 * k
        minutes.append((round(p1), round(p2)))

    awake = [m for m, (a, b) in enumerate(minutes) if a + b >= 3]
    first, last = (awake[0], awake[-1]) if awake else (None, None)
    return tuple(minutes), first, last


@lru_cache(maxsize=None)
def day_energy(d: date) -> tuple:
    """Running daily counters (e1, e2) in kWh at the end of every minute."""
    minutes, _, _ = day_profile(d)
    e1, e2, run1, run2 = [], [], 0.0, 0.0
    for a, b in minutes:
        run1 += a / 60000
        run2 += b / 60000
        e1.append(run1)
        e2.append(run2)
    return tuple(e1), tuple(e2)


@lru_cache(maxsize=None)
def lifetime_before(d: date) -> tuple:
    """Lifetime counters (te1, te2) at local midnight starting day d."""
    t1, t2 = PRE_MONITOR_KWH
    day = FIRST_DAY
    while day < d:
        e1, e2 = day_energy(day)
        t1, t2 = t1 + e1[-1], t2 + e2[-1]
        day += timedelta(days=1)
    return t1, t2


def reading(d: date, minute: int) -> dict:
    """What the inverter reports at the given minute: power, the daily
    counters e1/e2 and the lifetime counters te1/te2."""
    minutes, _, _ = day_profile(d)
    cum1, cum2 = day_energy(d)
    e1, e2 = cum1[minute], cum2[minute]
    t1, t2 = lifetime_before(d)
    p1, p2 = minutes[minute]
    return {"p1": p1, "e1": round(e1, 5), "te1": round(t1 + e1, 5),
            "p2": p2, "e2": round(e2, 5), "te2": round(t2 + e2, 5)}


def _ts(d: date, minute: int) -> int:
    return int(datetime(d.year, d.month, d.day, minute // 60, minute % 60).timestamp())


# --- build-db ----------------------------------------------------------------

# Mirrors the two tables of app/database.py this script writes. Everything
# else (aggregates, migrations) the monitor creates and backfills on startup.
SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    timestamp     INTEGER PRIMARY KEY,
    p1            REAL,
    p2            REAL,
    e1            REAL,
    e2            REAL,
    te1           REAL,
    te2           REAL,
    online        INTEGER,
    co2_g_per_kwh REAL,
    price_per_kwh REAL
);
CREATE TABLE IF NOT EXISTS device_info (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    device_id     TEXT,
    serial_number TEXT,
    firmware      TEXT,
    min_power     INTEGER,
    max_power     INTEGER,
    last_seen     INTEGER
);
"""


def build_db(path: str) -> None:
    if Path(path).exists():
        sys.exit(f"Refusing to overwrite existing file: {path}")
    now = datetime.now()
    today = now.date()
    now_minute = now.hour * 60 + now.minute
    rows = []
    d = FIRST_DAY
    while d <= today:
        minutes, first, last = day_profile(d)
        if first is not None:
            step = 1 if (today - d).days < DENSE_DAYS else SPARSE_STEP_MIN
            price = 0.32 if d < TARIFF_CHANGE else 0.35
            peak_minute = max(range(1440), key=lambda m: sum(minutes[m]))
            end = last if d < today else min(last, now_minute - 1)
            for m in range(first, end + 1):
                # Sparse days still carry their peak and final counters, so
                # the backfilled daily aggregates come out exact.
                if (m - first) % step and m not in (peak_minute, last):
                    continue
                r = reading(d, m)
                rows.append((_ts(d, m), r["p1"], r["p2"], r["e1"], r["e2"],
                             r["te1"], r["te2"], 1, CO2_G_PER_KWH, price))
            if d < today and step == 1:
                # The poller's single offline marker per night.
                rows.append((_ts(d, last) + 120, None, None, None, None,
                             None, None, 0, None, None))
        d += timedelta(days=1)

    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO measurements VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute(
        "INSERT INTO device_info (id, device_id, firmware, min_power, max_power,"
        " last_seen) VALUES (1, ?, ?, 30, ?, ?)",
        (DEVICE_ID, FIRMWARE, AC_LIMIT_W, int(now.timestamp())),
    )
    con.commit()
    con.close()
    years = {}
    d = FIRST_DAY
    while d < today:
        e1, e2 = day_energy(d)
        years[d.year] = years.get(d.year, 0.0) + e1[-1] + e2[-1]
        d += timedelta(days=1)
    summary = ", ".join(f"{y}: {kwh:.0f} kWh" for y, kwh in sorted(years.items()))
    print(f"Wrote {len(rows)} rows to {path} ({summary})")


# --- serve -------------------------------------------------------------------

class _FakeInverter(BaseHTTPRequestHandler):
    """The two local-API endpoints the monitor polls, in the EZ1-M format."""

    def do_GET(self):
        now = datetime.now()
        if self.path.startswith("/getOutputData"):
            data = reading(now.date(), now.hour * 60 + now.minute)
        elif self.path.startswith("/getDeviceInfo"):
            data = {"deviceId": DEVICE_ID, "devVer": FIRMWARE, "ssid": "demo",
                    "ipAddr": self.server.server_address[0], "minPower": "30",
                    "maxPower": str(AC_LIMIT_W)}
        else:
            self.send_error(404)
            return
        body = json.dumps({"data": data, "message": "SUCCESS",
                           "deviceId": DEVICE_ID}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def serve(port: int = 8050) -> None:
    lifetime_before(date.today())      # warm the cache before the first poll
    print(f"Fake EZ1-M API listening on :{port}", flush=True)
    ThreadingHTTPServer(("", port), _FakeInverter).serve_forever()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "build-db" and len(sys.argv) == 3:
        build_db(sys.argv[2])
    elif cmd == "serve":
        serve(int(sys.argv[2]) if len(sys.argv) > 2 else 8050)
    else:
        sys.exit(__doc__)
