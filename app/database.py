"""SQLite database layer for EZ1 measurements."""
import aiosqlite
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


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
    co2_g_per_kwh REAL
);

CREATE INDEX IF NOT EXISTS idx_timestamp ON measurements(timestamp);

CREATE TABLE IF NOT EXISTS device_info (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    device_id     TEXT,
    serial_number TEXT,
    min_power     INTEGER,
    max_power     INTEGER,
    last_seen     INTEGER
);

-- Long-term aggregates: survive detail-data retention so users can compare
-- across years even after raw measurements have been pruned.
CREATE TABLE IF NOT EXISTS monthly_aggregates (
    year                INTEGER NOT NULL,
    month               INTEGER NOT NULL,
    total_kwh           REAL NOT NULL DEFAULT 0,
    peak_w              INTEGER NOT NULL DEFAULT 0,
    days_with_data      INTEGER NOT NULL DEFAULT 0,
    avg_co2_g_per_kwh   REAL,    -- energy-weighted average CO2 factor
    last_updated        INTEGER NOT NULL,
    PRIMARY KEY (year, month)
);

CREATE TABLE IF NOT EXISTS yearly_aggregates (
    year                INTEGER PRIMARY KEY,
    total_kwh           REAL NOT NULL DEFAULT 0,
    peak_w              INTEGER NOT NULL DEFAULT 0,
    days_with_data      INTEGER NOT NULL DEFAULT 0,
    avg_co2_g_per_kwh   REAL,
    last_updated        INTEGER NOT NULL
);
"""


# Idempotent migrations for upgrades from earlier versions. Each statement
# must be wrapped in a try/except IF NOT EXISTS isn't usable for ALTER TABLE
# in SQLite. We catch the OperationalError "duplicate column name" so the
# migration is safe to run repeatedly.
MIGRATIONS = [
    "ALTER TABLE measurements        ADD COLUMN co2_g_per_kwh REAL",
    "ALTER TABLE monthly_aggregates  ADD COLUMN avg_co2_g_per_kwh REAL",
    "ALTER TABLE yearly_aggregates   ADD COLUMN avg_co2_g_per_kwh REAL",
]


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(SCHEMA)
            # Apply column-add migrations for existing databases. The CREATE
            # TABLE statements above cover fresh installs; ALTER TABLE covers
            # upgrades. Both paths converge to the same schema.
            for stmt in MIGRATIONS:
                try:
                    await db.execute(stmt)
                except Exception as e:
                    # "duplicate column name" — expected on already-migrated DBs.
                    # Anything else is logged but doesn't crash startup.
                    msg = str(e).lower()
                    if "duplicate column" not in msg:
                        import logging
                        logging.getLogger(__name__).warning(
                            f"Migration '{stmt}' failed: {e}"
                        )
            await db.commit()

    async def insert_measurement(self, timestamp, p1, p2, e1, e2, te1, te2,
                                 online, co2_g_per_kwh=None):
        """Insert a measurement. co2_g_per_kwh is the CO2 factor that was
        active at the time of measurement (gCO2eq/kWh). May be None when no
        carbon-intensity data source is configured or when the historical
        DB has rows from before the column existed.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO measurements
                   (timestamp, p1, p2, e1, e2, te1, te2, online, co2_g_per_kwh)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, p1, p2, e1, e2, te1, te2,
                 1 if online else 0, co2_g_per_kwh),
            )
            await db.commit()

    async def update_device_info(self, device_id, serial_number, min_power, max_power):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO device_info
                   (id, device_id, serial_number, min_power, max_power, last_seen)
                   VALUES (1, ?, ?, ?, ?, ?)""",
                (device_id, serial_number, min_power, max_power, int(datetime.now().timestamp())),
            )
            await db.commit()

    async def get_device_info(self) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM device_info WHERE id = 1")
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_latest(self) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM measurements ORDER BY timestamp DESC LIMIT 1"
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_latest_online(self) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM measurements
                   WHERE online = 1
                   ORDER BY timestamp DESC LIMIT 1"""
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_recent_avg_power(self, seconds: int = 300) -> Optional[float]:
        cutoff = int(datetime.now().timestamp()) - seconds
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """SELECT AVG(COALESCE(p1, 0) + COALESCE(p2, 0))
                   FROM measurements
                   WHERE timestamp >= ? AND online = 1""",
                (cutoff,),
            )
            row = await cur.fetchone()
            return row[0] if row and row[0] is not None else None

    async def get_range(self, start_ts: int, end_ts: int, bucket_seconds: int = 0) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if bucket_seconds <= 0:
                cur = await db.execute(
                    """SELECT timestamp, p1, p2, e1, e2, te1, te2, online
                       FROM measurements
                       WHERE timestamp BETWEEN ? AND ?
                       ORDER BY timestamp ASC""",
                    (start_ts, end_ts),
                )
            else:
                cur = await db.execute(
                    f"""SELECT
                          (timestamp / {bucket_seconds}) * {bucket_seconds} AS bucket_ts,
                          AVG(p1) AS p1,
                          AVG(p2) AS p2,
                          MAX(e1) AS e1,
                          MAX(e2) AS e2,
                          MAX(te1) AS te1,
                          MAX(te2) AS te2,
                          MAX(online) AS online
                       FROM measurements
                       WHERE timestamp BETWEEN ? AND ?
                       GROUP BY bucket_ts
                       ORDER BY bucket_ts ASC""",
                    (start_ts, end_ts),
                )
            rows = await cur.fetchall()
            if bucket_seconds > 0:
                return [{"timestamp": r["bucket_ts"], **{k: r[k] for k in ("p1","p2","e1","e2","te1","te2","online")}} for r in rows]
            return [dict(r) for r in rows]

    async def get_daily_totals(self, days: int = 30) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT
                      date(timestamp, 'unixepoch', 'localtime') AS day,
                      MAX(e1) AS e1_max,
                      MAX(e2) AS e2_max
                   FROM measurements
                   WHERE timestamp >= ?
                   GROUP BY day
                   ORDER BY day ASC""",
                (int((datetime.now() - timedelta(days=days)).timestamp()),),
            )
            rows = await cur.fetchall()
            return [
                {
                    "day": r["day"],
                    "kwh": (r["e1_max"] or 0) + (r["e2_max"] or 0),
                }
                for r in rows
            ]

    async def get_monthly_totals(self, months: int = 12) -> list[dict]:
        """kWh sum per calendar month over the last N months.

        Logic: per-day MAX(e1+e2) gives that day's total production
        (e1/e2 are daily counters that reset at midnight). Sum those
        per calendar month."""
        # Oversize the day window so we catch all days in the requested
        # month range, then group by year-month.
        cutoff_days = months * 32
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT
                      strftime('%Y-%m', timestamp, 'unixepoch', 'localtime') AS month,
                      date(timestamp, 'unixepoch', 'localtime') AS day,
                      MAX(COALESCE(e1, 0) + COALESCE(e2, 0)) AS day_kwh
                   FROM measurements
                   WHERE timestamp >= ? AND online = 1
                   GROUP BY day
                   ORDER BY day ASC""",
                (int((datetime.now() - timedelta(days=cutoff_days)).timestamp()),),
            )
            rows = await cur.fetchall()

        # Sum per month
        monthly: dict[str, float] = {}
        for r in rows:
            monthly[r["month"]] = monthly.get(r["month"], 0.0) + (r["day_kwh"] or 0.0)
        # Keep only the last `months` months and sort
        sorted_items = sorted(monthly.items())[-months:]
        return [{"month": k, "kwh": round(v, 3)} for k, v in sorted_items]

    async def get_total_energy(self) -> float:
        """Lifetime energy from the most recent successful poll."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT te1, te2 FROM measurements
                   WHERE online = 1 AND te1 IS NOT NULL AND te2 IS NOT NULL
                   ORDER BY timestamp DESC LIMIT 1"""
            )
            row = await cur.fetchone()
            if not row:
                return 0.0
            return (row["te1"] or 0) + (row["te2"] or 0)

    async def get_peak_today(self) -> float:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """SELECT MAX(COALESCE(p1, 0) + COALESCE(p2, 0))
                   FROM measurements
                   WHERE date(timestamp, 'unixepoch', 'localtime') = date('now', 'localtime')
                     AND online = 1"""
            )
            row = await cur.fetchone()
            return row[0] if row and row[0] is not None else 0.0

    async def delete_old_measurements(self, older_than_days: int) -> int:
        cutoff = int((datetime.now() - timedelta(days=older_than_days)).timestamp())
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM measurements WHERE timestamp < ?",
                (cutoff,),
            )
            await db.commit()
            return cur.rowcount or 0

    async def count_measurements(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM measurements")
            row = await cur.fetchone()
            return row[0] if row else 0

    # --- Long-term aggregates ----------------------------------------

    async def get_measurements_date_range(self) -> tuple[Optional[int], Optional[int]]:
        """Return (earliest_ts, latest_ts) from online measurements, or
        (None, None) if no data."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT MIN(timestamp), MAX(timestamp) FROM measurements WHERE online = 1"
            )
            row = await cur.fetchone()
            if not row or row[0] is None:
                return (None, None)
            return (row[0], row[1])

    async def recompute_month_aggregate(self, year: int, month: int) -> dict:
        """Recompute and upsert the aggregate for a single (year, month).

        Uses the same daily-MAX(e1+e2) logic as get_monthly_totals so the
        values are consistent with what the History chart shows. Returns
        the computed aggregate as a dict.

        Additionally computes the energy-weighted average CO2 factor —
        weighted by power output, so that periods of high production count
        more than idle/low-output periods. This is more accurate for the
        lifetime CO2 calculation than a time-weighted average, and
        automatically gives Solar-tilted production a low-CO2 bias since
        the user's production happens during sunshine hours when the grid
        is typically cleaner.

        Idempotent: safe to call repeatedly. Replaces the existing row.
        """
        start = datetime(year, month, 1)
        end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # Per-day totals (daily counters reset at midnight, MAX = day total)
            cur = await db.execute(
                """SELECT
                      date(timestamp, 'unixepoch', 'localtime') AS day,
                      MAX(COALESCE(e1, 0) + COALESCE(e2, 0)) AS day_kwh,
                      MAX(COALESCE(p1, 0) + COALESCE(p2, 0)) AS day_peak
                   FROM measurements
                   WHERE timestamp >= ? AND timestamp < ? AND online = 1
                   GROUP BY day""",
                (start_ts, end_ts),
            )
            rows = await cur.fetchall()

            total_kwh = sum((r["day_kwh"] or 0.0) for r in rows)
            peak_w = max((r["day_peak"] or 0) for r in rows) if rows else 0
            days_with_data = sum(1 for r in rows if (r["day_kwh"] or 0) > 0)

            # Energy-weighted CO2 average over all measurements with both a
            # known CO2 factor and non-zero power. NULL when there's no data.
            cur2 = await db.execute(
                """SELECT
                      SUM(co2_g_per_kwh * (COALESCE(p1, 0) + COALESCE(p2, 0))) AS weighted_sum,
                      SUM(COALESCE(p1, 0) + COALESCE(p2, 0)) AS power_sum
                   FROM measurements
                   WHERE timestamp >= ? AND timestamp < ?
                     AND online = 1
                     AND co2_g_per_kwh IS NOT NULL
                     AND (COALESCE(p1, 0) + COALESCE(p2, 0)) > 0""",
                (start_ts, end_ts),
            )
            co2_row = await cur2.fetchone()
            avg_co2 = None
            if co2_row and co2_row["power_sum"] and co2_row["power_sum"] > 0:
                avg_co2 = round(co2_row["weighted_sum"] / co2_row["power_sum"], 2)

            now_ts = int(datetime.now().timestamp())

            await db.execute(
                """INSERT OR REPLACE INTO monthly_aggregates
                   (year, month, total_kwh, peak_w, days_with_data,
                    avg_co2_g_per_kwh, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (year, month, round(total_kwh, 3), int(peak_w),
                 days_with_data, avg_co2, now_ts),
            )
            await db.commit()

            return {
                "year": year,
                "month": month,
                "total_kwh": round(total_kwh, 3),
                "peak_w": int(peak_w),
                "days_with_data": days_with_data,
                "avg_co2_g_per_kwh": avg_co2,
            }

    async def recompute_year_aggregate(self, year: int) -> dict:
        """Recompute and upsert the yearly aggregate from its monthly rows.

        Yearly CO2 average is energy-weighted from the monthly rows —
        weighted by each month's total kWh production so that high-output
        summer months count more than low-output winter months. This
        preserves the same physical meaning as the per-measurement
        weighting in recompute_month_aggregate.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT
                      COALESCE(SUM(total_kwh), 0)       AS total_kwh,
                      COALESCE(MAX(peak_w), 0)          AS peak_w,
                      COALESCE(SUM(days_with_data), 0)  AS days_with_data,
                      COALESCE(SUM(
                          CASE WHEN avg_co2_g_per_kwh IS NOT NULL
                               THEN avg_co2_g_per_kwh * total_kwh
                               ELSE 0 END), 0)          AS weighted_co2_sum,
                      COALESCE(SUM(
                          CASE WHEN avg_co2_g_per_kwh IS NOT NULL
                               THEN total_kwh
                               ELSE 0 END), 0)          AS co2_weight_sum
                   FROM monthly_aggregates
                   WHERE year = ?""",
                (year,),
            )
            row = await cur.fetchone()

            total_kwh = row["total_kwh"] if row else 0.0
            peak_w = row["peak_w"] if row else 0
            days_with_data = row["days_with_data"] if row else 0
            avg_co2 = None
            if row and row["co2_weight_sum"] and row["co2_weight_sum"] > 0:
                avg_co2 = round(row["weighted_co2_sum"] / row["co2_weight_sum"], 2)

            now_ts = int(datetime.now().timestamp())

            await db.execute(
                """INSERT OR REPLACE INTO yearly_aggregates
                   (year, total_kwh, peak_w, days_with_data,
                    avg_co2_g_per_kwh, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (year, round(total_kwh, 3), int(peak_w),
                 days_with_data, avg_co2, now_ts),
            )
            await db.commit()

            return {
                "year": year,
                "total_kwh": round(total_kwh, 3),
                "peak_w": int(peak_w),
                "days_with_data": days_with_data,
                "avg_co2_g_per_kwh": avg_co2,
            }

    async def get_monthly_aggregates(self, year: Optional[int] = None) -> list[dict]:
        """Read monthly_aggregates, optionally filtered to a single year."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if year is not None:
                cur = await db.execute(
                    """SELECT year, month, total_kwh, peak_w, days_with_data,
                              avg_co2_g_per_kwh
                       FROM monthly_aggregates
                       WHERE year = ?
                       ORDER BY month ASC""",
                    (year,),
                )
            else:
                cur = await db.execute(
                    """SELECT year, month, total_kwh, peak_w, days_with_data,
                              avg_co2_g_per_kwh
                       FROM monthly_aggregates
                       ORDER BY year ASC, month ASC"""
                )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_yearly_aggregates(self) -> list[dict]:
        """Read all yearly_aggregates ordered by year."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT year, total_kwh, peak_w, days_with_data,
                          avg_co2_g_per_kwh
                   FROM yearly_aggregates
                   ORDER BY year ASC"""
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_lifetime_co2_g(self) -> Optional[float]:
        """Sum of CO2 emissions (in grams) that would have been emitted by
        the grid for the energy this inverter produced. Calculated from
        the per-measurement values to be historically accurate — older
        measurements use the CO2 factor that was active at their time.

        Returns None if no measurements have a recorded CO2 factor (the
        caller can then fall back to total_kwh * static_factor).
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # We weight each measurement's CO2 factor by its power output,
            # then integrate using the measurement interval. Since the
            # interval can vary (adaptive polling), we approximate with
            # the energy delta from the per-day MAX, scaled by the
            # production-weighted CO2 average for that day.
            #
            # Concretely, per day:
            #   day_kwh         = MAX(e1+e2)
            #   day_avg_co2     = SUM(co2 * power) / SUM(power)
            #   day_co2_g       = day_kwh * day_avg_co2
            # And lifetime = SUM(day_co2_g) over all days that have CO2 data.
            cur = await db.execute(
                """WITH days AS (
                       SELECT
                           date(timestamp, 'unixepoch', 'localtime') AS day,
                           MAX(COALESCE(e1, 0) + COALESCE(e2, 0)) AS day_kwh,
                           SUM(co2_g_per_kwh * (COALESCE(p1, 0) + COALESCE(p2, 0)))
                               AS weighted_sum,
                           SUM(COALESCE(p1, 0) + COALESCE(p2, 0)) AS power_sum
                       FROM measurements
                       WHERE online = 1
                         AND co2_g_per_kwh IS NOT NULL
                         AND (COALESCE(p1, 0) + COALESCE(p2, 0)) > 0
                       GROUP BY day
                   )
                   SELECT COALESCE(SUM(
                       day_kwh * (weighted_sum / power_sum)
                   ), 0) AS lifetime_co2_g
                   FROM days
                   WHERE power_sum > 0"""
            )
            row = await cur.fetchone()
            if not row or not row["lifetime_co2_g"]:
                return None
            return float(row["lifetime_co2_g"])
