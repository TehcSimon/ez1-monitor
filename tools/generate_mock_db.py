"""Generate a SQLite DB pre-populated with realistic mock data, used to
test the Hall of Fame glow animations without waiting weeks for real
data to accumulate.

Writes one `measurements` row per day for the last 3 years plus a few
extra rows for today. The today rows are spaced 15 minutes apart with
an arc that peaks higher than any other day in the history, so the
all-time best-day slot in the Hall of Fame triggers "fresh" state and
glows endlessly with the NEW badge.

Usage:
    python3 tools/generate_mock_db.py /path/to/output.db

Then point a container at the resulting file:
    docker run ... -v /path/to:/data \\
                   -e DB_PATH=/data/output.db \\
                   ...

Or copy it into your appdata path before starting the container the
first time.

Important: this script writes only `measurements`. All aggregate
tables (daily, monthly, yearly) are derived by the container at
startup via the standard backfill. So the workflow is:

    1. Run this script to produce the .db file
    2. Place it where the container will find it
    3. Start the container — backfill populates aggregates in ~1 s
    4. Open the dashboard — Hall of Fame day-slot is "fresh"

If you want different glow states, edit the constants at the top:
- TODAY_BREAKS_DAY_RECORD       — fires "fresh" on the day tier
- WEEK_RECORD_DAYS_AGO          — fires "recent" on the week tier
- MONTH_RECORD_MONTHS_AGO       — fires "recent" on the month tier
- YEAR_RECORD_DONE_YEARS_AGO    — fires "recent" on the year tier
"""
import math
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

# --- Knobs you can twist ---------------------------------------------------

# All-time best-day record: when did it happen, and how big was it?
# If TODAY_BREAKS_DAY_RECORD is True, today's curve is sized to beat
# every other day in the dataset, triggering "fresh" state.
TODAY_BREAKS_DAY_RECORD = True
TODAY_PEAK_KWH = 9.5

# Week-record placement: how many days ago did the best week end?
# 0 = current week (fresh), 1-3 = recent (glows on load), >3 = settled
WEEK_RECORD_DAYS_AGO = 2

# Years of synthetic history (3 = unlocks the year-tier animation)
YEARS_OF_HISTORY = 3

# Inverter spec — used as the cap for the synthetic power arc
INSTALLED_KWP = 1.0
PEAK_AC_W = 800  # standard balcony PV cap

# Output column for CO2 — values around 350-400 g/kWh keep the lifetime
# CO2 number realistic without being the focus of this script
CO2_DEFAULT_G_PER_KWH = 400


# --- Schema -----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    timestamp        INTEGER NOT NULL PRIMARY KEY,
    p1               REAL,
    p2               REAL,
    e1               REAL,
    e2               REAL,
    te1              REAL,
    te2              REAL,
    online           INTEGER NOT NULL DEFAULT 1,
    co2_g_per_kwh    REAL,
    price_per_kwh    REAL
);

-- Mirrors the app's real device_info schema (app/database.py). The earlier
-- divergent column set made the poller's update_device_info fail when run
-- against a mock DB.
CREATE TABLE IF NOT EXISTS device_info (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    device_id        TEXT,
    serial_number    TEXT,
    firmware         TEXT,
    min_power        INTEGER,
    max_power        INTEGER,
    last_seen        INTEGER
);
"""


# --- Data generation --------------------------------------------------------

def seasonal_kwh(date: datetime) -> float:
    """Plausible daily total in kWh for a 1 kWp installation.

    Sinusoidal around the year, summer high, winter low. Multiplied by
    a per-day random factor between 0.3 (cloudy) and 1.0 (sunny).
    """
    day_of_year = date.timetuple().tm_yday
    # Summer solstice is around day 172. Cosine of (day - 172) gives 1
    # in summer, -1 in winter.
    seasonal = math.cos(2 * math.pi * (day_of_year - 172) / 365)
    # Map [-1, 1] to a kWh band: winter ~0.6, summer ~6.0
    base = 3.3 + seasonal * 2.7
    weather = random.uniform(0.3, 1.0)
    return max(0.05, base * weather)


def split_pv(total_kwh: float) -> tuple[float, float]:
    """Split total between PV1 and PV2 with slight asymmetry."""
    ratio = random.uniform(0.45, 0.55)
    return total_kwh * ratio, total_kwh * (1 - ratio)


def generate_measurements():
    """Yield (timestamp, p1, p2, e1, e2, te1, te2, online, co2) tuples."""
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    history_start = today_start - timedelta(days=YEARS_OF_HISTORY * 365)

    # Lifetime accumulators — must monotonically increase across days
    te1_running = 0.0
    te2_running = 0.0

    # Determine the historical peak so we know how high to push today
    historical_peaks = []
    saved_days = []
    cur = history_start
    while cur < today_start:
        kwh = seasonal_kwh(cur)
        # Track for record planning
        if WEEK_RECORD_DAYS_AGO > 0 and (today_start - cur).days == WEEK_RECORD_DAYS_AGO + 3:
            # Boost a day inside the target week to make it the best week
            kwh *= 1.6
        e1, e2 = split_pv(kwh)
        te1_running += e1
        te2_running += e2
        historical_peaks.append(kwh)
        # Emit one measurement at end-of-day (20:00 local) representing
        # the cumulative day total. This is enough for the backfill to
        # derive correct daily_aggregates rows.
        ts_eod = int((cur.replace(hour=20)).timestamp())
        # Per-channel power at peak time was around 200-450 W for a
        # productive day. Estimate from total kWh.
        peak_w_per_channel = min(PEAK_AC_W // 2, int(kwh * 60))
        yield (
            ts_eod,
            peak_w_per_channel * 0.7,   # p1 at 20:00 — already declining
            peak_w_per_channel * 0.6,   # p2 at 20:00
            round(e1, 3), round(e2, 3),
            round(te1_running, 3), round(te2_running, 3),
            1,
            CO2_DEFAULT_G_PER_KWH,
        )
        saved_days.append((cur, e1, e2))
        cur += timedelta(days=1)

    # --- Today ---------------------------------------------------------
    # Build a half-day curve that peaks higher than any historical day
    historical_max = max(historical_peaks) if historical_peaks else 5.0
    if TODAY_BREAKS_DAY_RECORD:
        target_today_kwh = max(TODAY_PEAK_KWH, historical_max * 1.10)
    else:
        target_today_kwh = historical_max * 0.7  # ordinary day

    # Spread today across 8 measurement points from 06:00 to 20:00,
    # 2-hour spacing, with a bell-curve of power
    hours = list(range(6, 21, 2))  # 06, 08, 10, 12, 14, 16, 18, 20
    n = len(hours)
    # Bell-shaped power profile: peak at ~13:00 = index ~3.5
    weights = [math.exp(-((i - 3.5) ** 2) / 4.5) for i in range(n)]
    weight_sum = sum(weights)
    # Energy delta per slot
    e_increments = [(w / weight_sum) * target_today_kwh for w in weights]
    # Power at peak — proportional to energy slope, capped at PEAK_AC_W/2
    cumulative_e1 = 0.0
    cumulative_e2 = 0.0
    for i, h in enumerate(hours):
        # Stop generating if this hour is in the future
        slot_dt = today_start.replace(hour=h)
        if slot_dt > now:
            break
        slot_kwh = e_increments[i]
        slot_p1, slot_p2 = split_pv(slot_kwh)
        cumulative_e1 += slot_p1
        cumulative_e2 += slot_p2
        te1_running += slot_p1
        te2_running += slot_p2
        # Approximate instantaneous power around this slot — 2h window
        # so kWh/2 * 1000 = avg W; scale by bell factor to make it peak
        avg_w_total = (slot_kwh / 2.0) * 1000
        p1 = min(PEAK_AC_W * 0.5, avg_w_total * 0.5)
        p2 = min(PEAK_AC_W * 0.5, avg_w_total * 0.5)
        yield (
            int(slot_dt.timestamp()),
            round(p1, 1), round(p2, 1),
            round(cumulative_e1, 3), round(cumulative_e2, 3),
            round(te1_running, 3), round(te2_running, 3),
            1,
            CO2_DEFAULT_G_PER_KWH,
        )


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <output.db>", file=sys.stderr)
        sys.exit(1)

    out_path = sys.argv[1]
    if os.path.exists(out_path):
        print(f"Refusing to overwrite existing file: {out_path}", file=sys.stderr)
        sys.exit(1)

    random.seed(42)  # deterministic curves so re-runs match

    conn = sqlite3.connect(out_path)
    conn.executescript(SCHEMA)

    count = 0
    for row in generate_measurements():
        conn.execute(
            """INSERT OR REPLACE INTO measurements
               (timestamp, p1, p2, e1, e2, te1, te2, online,
                co2_g_per_kwh, price_per_kwh)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.35)""",
            row,
        )
        count += 1

    # Fake device_info so /api/live has something to show
    conn.execute(
        """INSERT OR REPLACE INTO device_info
           (id, device_id, firmware, min_power, max_power, last_seen)
           VALUES (1, 'MOCK001', '1.7.0', 30, 800, strftime('%s','now'))"""
    )

    conn.commit()
    conn.close()

    print(f"Wrote {count} measurement rows over {YEARS_OF_HISTORY} years to {out_path}")
    print()
    print("Next steps:")
    print(f"  1. Place {out_path} somewhere your container can read")
    print(f"  2. Start the container with DB_PATH pointing at it")
    print(f"  3. On first start the container runs aggregate backfill (~1 s)")
    print(f"  4. Open the dashboard — Hall of Fame day-slot will be 'fresh'")


if __name__ == "__main__":
    main()
