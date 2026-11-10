"""Synthetic balcony plant for the README screenshots.

One deterministic model feeds both halves of a realistic demo setup:

    python demo.py build-db /data/ez1.db   # history → demo SQLite database
    python demo.py build-site /data/x.json # made-up surroundings and forecast for the sun scene
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
import bisect
import hashlib
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
# A clear summer day of a real, partly shaded balcony plant, as (minute of the
# day, PV 1 watts, PV 2 watts): diffuse light only until the sun clears a
# building around 10:00, the shadow reaching PV 1 at about 14:00 and PV 2 at
# about 17:30. Every other day is this curve, squeezed into its daylight hours
# and scaled by season and weather.
CLEAR_DAY = (
    (360, 0, 0), (390, 5, 5), (420, 7, 8), (480, 12, 12), (540, 16, 14),
    (570, 40, 22), (590, 90, 35), (598, 275, 40), (607, 288, 55), (608, 290, 270),
    (630, 305, 290), (660, 340, 320), (690, 368, 350), (715, 388, 352),
    (750, 365, 338), (780, 358, 332), (820, 365, 342), (830, 350, 345),
    (845, 200, 345), (855, 120, 350), (870, 65, 362), (889, 58, 376),
    (930, 55, 350), (960, 55, 328), (990, 52, 298), (1020, 50, 268),
    (1032, 48, 160), (1050, 45, 140), (1060, 40, 50), (1080, 35, 45),
    (1110, 30, 30), (1140, 22, 25), (1200, 12, 12), (1260, 3, 3), (1275, 0, 0),
)
# Its share of the (model) daylight hours, and its midpoint after solar noon.
CLEAR_DAY_SPAN, CLEAR_DAY_MID_AFTER_NOON_H = 0.924, 0.29
# PV 1 flickers while the edge of the shadow moves across it.
SHADOW_EDGE_MINUTES = (825, 870)
# The anniversary of the record day, which the demo's clock is pinned to.
BEST_DAY = (6, 24)


def _clear_day(minute: float) -> tuple:
    """(p1, p2) of the CLEAR_DAY curve at the given minute of that day."""
    i = bisect.bisect_right([point[0] for point in CLEAR_DAY], minute)
    if i == 0 or i == len(CLEAR_DAY):
        return 0.0, 0.0
    (m0, a0, b0), (m1, a1, b1) = CLEAR_DAY[i - 1], CLEAR_DAY[i]
    f = (minute - m0) / (m1 - m0)
    return a0 + (a1 - a0) * f, b0 + (b1 - b0) * f


def _season(d: date) -> float:
    """+1 at the summer solstice, -1 at the winter solstice."""
    return math.cos(2 * math.pi * (d.timetuple().tm_yday - 172) / 365.25)


def _dst_hours(d: date) -> int:
    noon = datetime(d.year, d.month, d.day, 12).timestamp()
    return 1 if time.localtime(noon).tm_isdst > 0 else 0


@lru_cache(maxsize=None)
def day_profile(d: date) -> tuple:
    """Per-minute (p1, p2) watts of local wall-clock day d, plus the first
    and last minute the inverter is awake. The CLEAR_DAY curve under that
    day's (seeded) weather."""
    rng = random.Random(d.toordinal() * 7919)
    s = _season(d)
    daylight_h = 12.2 + 4.3 * s
    noon_h = 12 + 20 / 60 + _dst_hours(d)      # solar noon at ~10° E
    span_h = CLEAR_DAY_SPAN * daylight_h
    rise_h = noon_h + CLEAR_DAY_MID_AFTER_NOON_H - span_h / 2
    strength = 0.72 + 0.28 * s                 # lower winter sun

    sunny_odds = 0.38 + 0.2 * s                # grey winters, sunnier summers
    roll = rng.random()
    # A sunny streak into "today" (the pinned demo date, see compose.yml)
    # gives the live curve, the week view and the record-pace pill
    # something to show. Only the record day itself reaches the full curve.
    if (d.month, d.day) == BEST_DAY:
        kind, level = "sunny", 1.0
    elif 0 <= (date.today() - d).days < 4 or roll < sunny_odds:
        kind, level = "sunny", rng.uniform(0.82, 0.97)
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
        x = (h - rise_h) / span_h
        curve_minute = CLEAR_DAY[0][0] + x * (CLEAR_DAY[-1][0] - CLEAR_DAY[0][0])
        clear1, clear2 = _clear_day(curve_minute)
        p1, p2 = clear1 * strength * t, clear2 * strength * t
        if kind == "sunny" and SHADOW_EDGE_MINUTES[0] < curve_minute < SHADOW_EDGE_MINUTES[1]:
            p1 *= rng.uniform(0.8, 1.0)
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
    price_per_kwh REAL,
    temp_c        REAL
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
                             r["te1"], r["te2"], 1, CO2_G_PER_KWH, price, _temperature(r)))
            if d < today and step == 1:
                # The poller's single offline marker per night.
                rows.append((_ts(d, last) + 120, None, None, None, None,
                             None, None, 0, None, None, None))
        d += timedelta(days=1)

    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO measurements VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
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

def _temperature(r: dict) -> int:
    """The inverter's temperature, warmer under load."""
    return round(23 + 0.022 * (r["p1"] + r["p2"]))


def detail(d: date, minute: int) -> dict:
    """The reading plus temperature and grid values."""
    r = reading(d, minute)
    rng = random.Random(d.toordinal() * 1440 + minute)
    return {**r, "t": _temperature(r),
            "gv": round(232 + rng.uniform(-2, 4)),
            "gf": round(50 + rng.uniform(-0.03, 0.03), 2)}


class _FakeInverter(BaseHTTPRequestHandler):
    """The local-API endpoints the monitor polls, in the EZ1-M format."""

    def do_GET(self):
        now = datetime.now()
        minute = now.hour * 60 + now.minute
        if self.path.startswith("/getOutputDataDetail"):
            data = detail(now.date(), minute)
        elif self.path.startswith("/getOutputData"):
            data = reading(now.date(), minute)
        elif self.path.startswith("/getMaxPower"):
            data = {"maxPower": str(AC_LIMIT_W)}
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


# --- The demo surroundings ---------------------------------------------------

def _block(x0, y0, x1, y1, height, home=False):
    item = {"f": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "h": height}
    return {**item, "home": 1} if home else item


def build_site(path):
    """A made-up street corner in the sun scene's cache format, so the
    screenshots show the feature without showing anybody's real neighbourhood.
    The monitor finds it under SITE_ADDRESS=Demo and asks no outside service."""
    surroundings = {
        "lat": 51.3, "lon": 9.5,
        "buildings": [
            _block(-13, -6.5, 13, 6.5, 10, home=True),
            _block(-40, -7, -22, 6, 8),
            _block(-20, 20, -4, 35, 8),
            _block(4, 20, 20, 34, 11),
            _block(-20, -43, -6, -32, 8),
            _block(0, -42, 14, -32, 8),
            _block(39, 10, 44, 22, 8),
            _block(40, -12, 46, 6, 10),
            _block(16, -4, 23, 3, 3),
            _block(-19, 12, -13, 18, 3),
        ],
        "roads": [
            {"w": 7, "p": [[-90, -24], [33, -24], [90, -24]]},
            {"w": 6, "p": [[33, -24], [33, 90]]},
        ],
        "labels": [
            {"t": "Lindenweg", "p": [[x, -24] for x in range(-40, 41, 8)]},
            {"t": "Am Hang", "p": [[33, y] for y in range(-22, 33, 6)]},
        ],
    }
    Path(path).write_text(json.dumps({
        "version": 1,
        "address": hashlib.sha256(b"Demo").hexdigest(),
        "lat": surroundings["lat"], "lon": surroundings["lon"],
        "surroundings": surroundings,
    }), encoding="utf-8")
    build_weather(Path(path).with_name("weather.json"), surroundings["lat"], surroundings["lon"])


# WMO code by hour of the day, so the scene has some weather to draw: a
# clear morning, clouds building up in the afternoon, showers in the evening.
DEMO_SKY = [0] * 10 + [1, 1, 1, 2, 2, 2, 3, 3, 80, 61, 2, 1, 0, 0]


def build_weather(path, lat, lon):
    """A forecast in the weather cache's format, its radiation taken from the
    same model as the power. A restart within the hour reuses that file, so
    the monitor doesn't ask Open-Meteo during the shots."""
    times, radiation, codes, temperatures = [], [], [], []
    for days_ahead in range(-23, 4):
        d = date.today() + timedelta(days=days_ahead)
        minutes = day_profile(d)[0]
        for hour in range(24):
            times.append(_ts(d, hour * 60) + 3600)
            radiation.append(round(sum(a + b for a, b in minutes[hour * 60:(hour + 1) * 60]) / 60, 1))
            codes.append(DEMO_SKY[hour])
            # Coolest around sunrise, warmest in the late afternoon.
            temperatures.append(round(17 + 7 * math.sin((hour + 1 - 10) / 24 * 2 * math.pi), 1))
    Path(path).write_text(json.dumps({
        "version": 1, "lat": lat, "lon": lon, "fetched_at": time.time(),
        "times": times, "radiation": radiation,
        "codes": codes, "temperatures": temperatures,
    }), encoding="utf-8")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "build-db" and len(sys.argv) == 3:
        build_db(sys.argv[2])
    elif cmd == "build-site" and len(sys.argv) == 3:
        build_site(sys.argv[2])
    elif cmd == "serve":
        serve(int(sys.argv[2]) if len(sys.argv) > 2 else 8050)
    else:
        sys.exit(__doc__)
