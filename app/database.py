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
    co2_g_per_kwh REAL,
    price_per_kwh REAL
);

CREATE INDEX IF NOT EXISTS idx_timestamp ON measurements(timestamp);

CREATE TABLE IF NOT EXISTS device_info (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    device_id     TEXT,
    serial_number TEXT,
    firmware      TEXT,
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
    avg_price_per_kwh   REAL,    -- energy-weighted average electricity price
    last_updated        INTEGER NOT NULL,
    PRIMARY KEY (year, month)
);

CREATE TABLE IF NOT EXISTS yearly_aggregates (
    year                INTEGER PRIMARY KEY,
    total_kwh           REAL NOT NULL DEFAULT 0,
    peak_w              INTEGER NOT NULL DEFAULT 0,
    days_with_data      INTEGER NOT NULL DEFAULT 0,
    avg_co2_g_per_kwh   REAL,
    avg_price_per_kwh   REAL,
    last_updated        INTEGER NOT NULL
);

-- Daily aggregates: one row per calendar day with that day's total energy.
-- Used for "best day" Hall of Fame highscores so they survive raw-row
-- retention pruning. Date stored as ISO string (YYYY-MM-DD) for natural
-- sort and easy display.
CREATE TABLE IF NOT EXISTS daily_aggregates (
    date                TEXT PRIMARY KEY,     -- YYYY-MM-DD
    total_kwh           REAL NOT NULL DEFAULT 0,
    peak_w              INTEGER NOT NULL DEFAULT 0,
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
    # v1.6.1: per-measurement electricity price stamping (same pattern as
    # the CO2 factor) and firmware version in device_info.
    "ALTER TABLE measurements        ADD COLUMN price_per_kwh REAL",
    "ALTER TABLE monthly_aggregates  ADD COLUMN avg_price_per_kwh REAL",
    "ALTER TABLE yearly_aggregates   ADD COLUMN avg_price_per_kwh REAL",
    "ALTER TABLE device_info         ADD COLUMN firmware TEXT",
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
                                 online, co2_g_per_kwh=None, price_per_kwh=None):
        """Insert a measurement. co2_g_per_kwh is the CO2 factor that was
        active at the time of measurement (gCO2eq/kWh). May be None when no
        carbon-intensity data source is configured or when the historical
        DB has rows from before the column existed.

        price_per_kwh is the electricity price that was active at the time
        (configured PRICE_PER_KWH). Stamping it per row keeps the lifetime
        "money saved" calculation historically accurate across tariff
        changes — the same pattern as the CO2 factor. May be None for rows
        from before the column existed.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO measurements
                   (timestamp, p1, p2, e1, e2, te1, te2, online,
                    co2_g_per_kwh, price_per_kwh)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, p1, p2, e1, e2, te1, te2,
                 1 if online else 0, co2_g_per_kwh, price_per_kwh),
            )
            await db.commit()

    async def update_device_info(self, device_id, firmware, min_power, max_power):
        """Upsert the single device_info row.

        device_id is the inverter's ID (effectively its serial number).
        firmware is the devVer string from the local API. The legacy
        serial_number column is kept in the schema but no longer written —
        it used to (incorrectly) hold the firmware version.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO device_info
                   (id, device_id, firmware, min_power, max_power, last_seen)
                   VALUES (1, ?, ?, ?, ?, ?)""",
                (device_id, firmware, min_power, max_power, int(datetime.now().timestamp())),
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

    async def get_energy_in_windows(
        self, windows: list[tuple[int, int]]
    ) -> list[float]:
        """Compute the kWh produced in each (start_ts, end_ts) window.

        All windows are queried in a single database connection, which is
        substantially faster than calling get_range() per window because
        we avoid the per-call connect overhead. Each window is still its
        own query — that's fine since the timestamp index makes each
        range scan O(log N).

        The kWh value is the sum of per-day MAX(e1) + MAX(e2). The e1/e2
        columns are the daily-resetting energy counters from the
        inverter, so MAX-per-day yields each day's total production and
        SUM across days yields the window total.
        """
        results: list[float] = []
        async with aiosqlite.connect(self.db_path) as db:
            for start_ts, end_ts in windows:
                cur = await db.execute(
                    """SELECT
                          COALESCE(SUM(daily_e1), 0) + COALESCE(SUM(daily_e2), 0) AS kwh
                       FROM (
                         SELECT
                           MAX(e1) AS daily_e1,
                           MAX(e2) AS daily_e2
                         FROM measurements
                         WHERE timestamp BETWEEN ? AND ?
                         GROUP BY (timestamp / 86400)
                       )""",
                    (start_ts, end_ts),
                )
                row = await cur.fetchone()
                results.append(float(row[0] or 0))
        return results

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
                # bucket_seconds is bound as a parameter rather than
                # interpolated into the SQL string. It's an internal int
                # so injection isn't a real concern, but parameterized
                # binding is the correct pattern and lets SQLite cache
                # the prepared statement across different bucket sizes.
                cur = await db.execute(
                    """SELECT
                          (timestamp / ?) * ? AS bucket_ts,
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
                    (bucket_seconds, bucket_seconds, start_ts, end_ts),
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

    async def get_peak_today_with_time(self) -> tuple[float, Optional[int]]:
        """Return (peak_watts, unix_timestamp_of_peak) for today.

        Used by the hero card to show "Peak today: 612 W at 13:24" instead
        of just the bare number.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """SELECT COALESCE(p1, 0) + COALESCE(p2, 0) AS power, timestamp
                   FROM measurements
                   WHERE date(timestamp, 'unixepoch', 'localtime') = date('now', 'localtime')
                     AND online = 1
                   ORDER BY power DESC, timestamp ASC
                   LIMIT 1"""
            )
            row = await cur.fetchone()
            if not row or row[0] is None or row[0] == 0:
                return 0.0, None
            return float(row[0]), int(row[1])

    async def get_today_production_window(
        self, threshold_w: int = 5
    ) -> tuple[Optional[int], Optional[int]]:
        """Return (first_ts, last_ts) of today's production above threshold.

        Production is anything above threshold_w on (p1+p2). Used to compute
        the "average during production" metric in the hero card. Returns
        (None, None) if nothing has been generated today yet.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """SELECT MIN(timestamp), MAX(timestamp)
                   FROM measurements
                   WHERE date(timestamp, 'unixepoch', 'localtime') = date('now', 'localtime')
                     AND online = 1
                     AND (COALESCE(p1, 0) + COALESCE(p2, 0)) >= ?""",
                (threshold_w,),
            )
            row = await cur.fetchone()
            if not row or row[0] is None:
                return None, None
            return int(row[0]), int(row[1])

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

            # Energy-weighted CO2 and price averages over all measurements
            # with a known factor and non-zero power. NULL when there's no
            # data for the respective factor. The two factors are weighted
            # independently because their NULL-eras differ (CO2 stamping
            # started in v1.4, price stamping in v1.6.1).
            cur2 = await db.execute(
                """SELECT
                      SUM(CASE WHEN co2_g_per_kwh IS NOT NULL
                           THEN co2_g_per_kwh * (COALESCE(p1, 0) + COALESCE(p2, 0))
                           END) AS co2_weighted_sum,
                      SUM(CASE WHEN co2_g_per_kwh IS NOT NULL
                           THEN COALESCE(p1, 0) + COALESCE(p2, 0)
                           END) AS co2_power_sum,
                      SUM(CASE WHEN price_per_kwh IS NOT NULL
                           THEN price_per_kwh * (COALESCE(p1, 0) + COALESCE(p2, 0))
                           END) AS price_weighted_sum,
                      SUM(CASE WHEN price_per_kwh IS NOT NULL
                           THEN COALESCE(p1, 0) + COALESCE(p2, 0)
                           END) AS price_power_sum
                   FROM measurements
                   WHERE timestamp >= ? AND timestamp < ?
                     AND online = 1
                     AND (COALESCE(p1, 0) + COALESCE(p2, 0)) > 0""",
                (start_ts, end_ts),
            )
            factor_row = await cur2.fetchone()
            avg_co2 = None
            avg_price = None
            if factor_row:
                if factor_row["co2_power_sum"] and factor_row["co2_power_sum"] > 0:
                    avg_co2 = round(
                        factor_row["co2_weighted_sum"] / factor_row["co2_power_sum"], 2
                    )
                if factor_row["price_power_sum"] and factor_row["price_power_sum"] > 0:
                    avg_price = round(
                        factor_row["price_weighted_sum"] / factor_row["price_power_sum"], 4
                    )

            now_ts = int(datetime.now().timestamp())

            await db.execute(
                """INSERT OR REPLACE INTO monthly_aggregates
                   (year, month, total_kwh, peak_w, days_with_data,
                    avg_co2_g_per_kwh, avg_price_per_kwh, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (year, month, round(total_kwh, 3), int(peak_w),
                 days_with_data, avg_co2, avg_price, now_ts),
            )
            await db.commit()

            return {
                "year": year,
                "month": month,
                "total_kwh": round(total_kwh, 3),
                "peak_w": int(peak_w),
                "days_with_data": days_with_data,
                "avg_co2_g_per_kwh": avg_co2,
                "avg_price_per_kwh": avg_price,
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
                               ELSE 0 END), 0)          AS co2_weight_sum,
                      COALESCE(SUM(
                          CASE WHEN avg_price_per_kwh IS NOT NULL
                               THEN avg_price_per_kwh * total_kwh
                               ELSE 0 END), 0)          AS weighted_price_sum,
                      COALESCE(SUM(
                          CASE WHEN avg_price_per_kwh IS NOT NULL
                               THEN total_kwh
                               ELSE 0 END), 0)          AS price_weight_sum
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
            avg_price = None
            if row and row["price_weight_sum"] and row["price_weight_sum"] > 0:
                avg_price = round(row["weighted_price_sum"] / row["price_weight_sum"], 4)

            now_ts = int(datetime.now().timestamp())

            await db.execute(
                """INSERT OR REPLACE INTO yearly_aggregates
                   (year, total_kwh, peak_w, days_with_data,
                    avg_co2_g_per_kwh, avg_price_per_kwh, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (year, round(total_kwh, 3), int(peak_w),
                 days_with_data, avg_co2, avg_price, now_ts),
            )
            await db.commit()

            return {
                "year": year,
                "total_kwh": round(total_kwh, 3),
                "peak_w": int(peak_w),
                "days_with_data": days_with_data,
                "avg_co2_g_per_kwh": avg_co2,
                "avg_price_per_kwh": avg_price,
            }

    async def get_monthly_aggregates(self, year: Optional[int] = None) -> list[dict]:
        """Read monthly_aggregates, optionally filtered to a single year."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if year is not None:
                cur = await db.execute(
                    """SELECT year, month, total_kwh, peak_w, days_with_data,
                              avg_co2_g_per_kwh, avg_price_per_kwh
                       FROM monthly_aggregates
                       WHERE year = ?
                       ORDER BY month ASC""",
                    (year,),
                )
            else:
                cur = await db.execute(
                    """SELECT year, month, total_kwh, peak_w, days_with_data,
                              avg_co2_g_per_kwh, avg_price_per_kwh
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
                          avg_co2_g_per_kwh, avg_price_per_kwh
                   FROM yearly_aggregates
                   ORDER BY year ASC"""
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # --- Daily aggregates -------------------------------------------------
    #
    # These power the Hall of Fame "best day" highscore. Daily totals are
    # stored permanently — they survive RETENTION_DAYS pruning of raw rows.

    async def upsert_daily_aggregate(
        self, date_iso: str, total_kwh: float, peak_w: int
    ) -> None:
        """Insert or replace a single day's aggregate."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO daily_aggregates
                   (date, total_kwh, peak_w, last_updated)
                   VALUES (?, ?, ?, strftime('%s','now'))""",
                (date_iso, total_kwh, peak_w),
            )
            await db.commit()

    async def backfill_daily_aggregates(self, since_iso: Optional[str] = None) -> int:
        """Populate daily_aggregates from existing measurements.

        Idempotent — called at startup and hourly. Walks the measurements
        table, groups by LOCAL calendar date (same 'localtime' semantics as
        every other daily query in this module — using UTC here attributed
        evening production to the wrong day for timezones west of UTC),
        and writes one row per day.

        since_iso optionally restricts the rewrite to days >= that ISO date.
        The caller passes the retention boundary so days whose raw rows have
        been partially pruned keep their previously stored (complete)
        aggregate instead of being overwritten with a reduced peak.

        Returns the number of day rows written.
        """
        params: list = []
        since_clause = ""
        if since_iso:
            since_clause = "AND date(timestamp, 'unixepoch', 'localtime') >= ?"
            params.append(since_iso)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"""SELECT date(timestamp, 'unixepoch', 'localtime') AS d,
                          MAX(e1) AS e1, MAX(e2) AS e2,
                          MAX(COALESCE(p1, 0) + COALESCE(p2, 0)) AS peak
                   FROM measurements
                   WHERE online = 1 {since_clause}
                   GROUP BY d
                   ORDER BY d ASC""",
                params,
            )
            rows = await cur.fetchall()
            for r in rows:
                d, e1, e2, peak = r
                if d is None:
                    continue
                total = float((e1 or 0) + (e2 or 0))
                peak_w = int(peak or 0)
                await db.execute(
                    """INSERT OR REPLACE INTO daily_aggregates
                       (date, total_kwh, peak_w, last_updated)
                       VALUES (?, ?, ?, strftime('%s','now'))""",
                    (d, total, peak_w),
                )
            await db.commit()
            return len(rows)

    async def get_best_day(self) -> Optional[dict]:
        """Return the all-time best day, or None if no data."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT date, total_kwh, peak_w
                   FROM daily_aggregates
                   ORDER BY total_kwh DESC, date DESC
                   LIMIT 1"""
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_best_day_in_range(
        self, start_iso: str, end_iso: str
    ) -> Optional[dict]:
        """Return the best day with date in [start_iso, end_iso] (inclusive)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT date, total_kwh, peak_w
                   FROM daily_aggregates
                   WHERE date BETWEEN ? AND ?
                   ORDER BY total_kwh DESC, date DESC
                   LIMIT 1""",
                (start_iso, end_iso),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_best_week(self) -> Optional[dict]:
        """Return the all-time best ISO calendar week (Mon-Sun).

        Returns dict with keys: year_week (ISO 'YYYY-Www'), iso_year, iso_week,
        week_start (Monday ISO date), total_kwh.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # SQLite has %Y/%W but %W treats week 00 as days before first
            # Monday. We compute ISO-year and ISO-week in Python instead so
            # the semantics match the rest of the codebase. Pull all days,
            # group by ISO week locally — cheap, < a few thousand rows.
            cur = await db.execute(
                "SELECT date, total_kwh FROM daily_aggregates ORDER BY date ASC"
            )
            rows = await cur.fetchall()

        if not rows:
            return None
        from datetime import date as date_cls
        from collections import defaultdict
        by_week: dict[tuple, float] = defaultdict(float)
        by_week_start: dict[tuple, str] = {}
        for r in rows:
            try:
                d = date_cls.fromisoformat(r["date"])
            except ValueError:
                continue
            iso_year, iso_week, _ = d.isocalendar()
            key = (iso_year, iso_week)
            by_week[key] += float(r["total_kwh"] or 0)
            # Track the Monday of the week (earliest day in that ISO week)
            if key not in by_week_start:
                # Monday of this ISO week
                jan4 = date_cls(iso_year, 1, 4)
                week1_monday = jan4 - timedelta(days=jan4.isoweekday() - 1)
                monday = week1_monday + timedelta(weeks=iso_week - 1)
                by_week_start[key] = monday.isoformat()
        if not by_week:
            return None
        best_key = max(by_week.keys(), key=lambda k: (by_week[k], k))
        iso_year, iso_week = best_key
        return {
            "iso_year": iso_year,
            "iso_week": iso_week,
            "year_week": f"{iso_year}-W{iso_week:02d}",
            "week_start": by_week_start[best_key],
            "total_kwh": by_week[best_key],
        }

    async def get_best_month(self) -> Optional[dict]:
        """Return the all-time best month from monthly_aggregates."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT year, month, total_kwh
                   FROM monthly_aggregates
                   ORDER BY total_kwh DESC, year DESC, month DESC
                   LIMIT 1"""
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_best_year(self) -> Optional[dict]:
        """Return the all-time best year from yearly_aggregates."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT year, total_kwh
                   FROM yearly_aggregates
                   ORDER BY total_kwh DESC, year DESC
                   LIMIT 1"""
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_data_extent(self) -> dict:
        """Return (first_date, last_date, days_with_data, completed_weeks,
        completed_months, completed_years) from daily_aggregates.

        Used for tier-unlock logic in the Hall of Fame: we don't show
        record animations until there's a meaningful amount of comparison
        data, so a fresh install doesn't blink every day for the first
        few weeks.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT MIN(date) AS first_date,
                          MAX(date) AS last_date,
                          COUNT(*) AS days
                   FROM daily_aggregates"""
            )
            row = await cur.fetchone()
            first = row["first_date"] if row else None
            last = row["last_date"] if row else None
            days = row["days"] if row else 0

            # Distinct completed ISO weeks (excluding the currently-running one)
            cur2 = await db.execute("SELECT date FROM daily_aggregates")
            all_dates = await cur2.fetchall()

        from datetime import date as date_cls
        today = date_cls.today()
        current_iso = today.isocalendar()
        completed_weeks: set = set()
        completed_months: set = set()
        completed_years: set = set()
        for r in all_dates:
            try:
                d = date_cls.fromisoformat(r["date"])
            except ValueError:
                continue
            iy, iw, _ = d.isocalendar()
            # Only count weeks/months/years that have ended
            if (iy, iw) != (current_iso[0], current_iso[1]):
                completed_weeks.add((iy, iw))
            if (d.year, d.month) != (today.year, today.month):
                completed_months.add((d.year, d.month))
            if d.year != today.year:
                completed_years.add(d.year)

        return {
            "first_date": first,
            "last_date": last,
            "days_with_data": days,
            "completed_weeks": len(completed_weeks),
            "completed_months": len(completed_months),
            "completed_years": len(completed_years),
        }

    async def get_lifetime_factor_split(
        self, current_year: int, current_month: int
    ) -> dict:
        """Lifetime energy split for the CO2 and money calculations.

        Returns, independently for each factor (CO2 and price), how many
        lifetime kWh have a known factor attached and the factor-weighted
        total over that share:

            {
              "co2_kwh":   kWh with a known CO2 factor,
              "co2_g":     grams of CO2 over that share,
              "price_kwh": kWh with a known price,
              "price_sum": currency units over that share,
            }

        The caller combines each share with the static fallback for the
        unmeasured remainder:

            unmeasured_kwh = total_kwh - measured_kwh
            total          = measured + unmeasured_kwh * static_factor

        Two sources are merged:

        1. monthly_aggregates for every month EXCEPT the current one.
           These survive RETENTION_DAYS pruning, so accuracy is preserved
           even after the underlying raw measurements are gone. (The
           previous implementation read raw measurements only, which made
           pruned energy silently fall back to the static factor.)
        2. The current month live from measurements, with the same
           per-day energy weighting used by recompute_month_aggregate.
           This keeps today's production reflected immediately instead of
           waiting for the hourly aggregate refresh.

        Months/rows where a factor is NULL (pre-v1.4 for CO2, pre-v1.6.1
        for price) contribute to neither share and thus fall back to the
        static factor — over time both shares grow toward 100%.
        """
        month_start = datetime(current_year, current_month, 1)
        month_start_ts = int(month_start.timestamp())

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # 1) Completed months from the long-term aggregates
            cur = await db.execute(
                """SELECT
                      COALESCE(SUM(CASE WHEN avg_co2_g_per_kwh IS NOT NULL
                                   THEN total_kwh END), 0) AS co2_kwh,
                      COALESCE(SUM(CASE WHEN avg_co2_g_per_kwh IS NOT NULL
                                   THEN avg_co2_g_per_kwh * total_kwh END), 0) AS co2_g,
                      COALESCE(SUM(CASE WHEN avg_price_per_kwh IS NOT NULL
                                   THEN total_kwh END), 0) AS price_kwh,
                      COALESCE(SUM(CASE WHEN avg_price_per_kwh IS NOT NULL
                                   THEN avg_price_per_kwh * total_kwh END), 0) AS price_sum
                   FROM monthly_aggregates
                   WHERE NOT (year = ? AND month = ?)""",
                (current_year, current_month),
            )
            agg = await cur.fetchone()

            # 2) Current month live from raw measurements. Per-day energy
            # weighting: day_kwh × (power-weighted factor of that day).
            cur2 = await db.execute(
                """WITH days AS (
                       SELECT
                           date(timestamp, 'unixepoch', 'localtime') AS day,
                           MAX(COALESCE(e1, 0) + COALESCE(e2, 0))    AS day_kwh,
                           SUM(CASE WHEN co2_g_per_kwh IS NOT NULL
                                THEN co2_g_per_kwh * (COALESCE(p1, 0) + COALESCE(p2, 0))
                                END) AS co2_wsum,
                           SUM(CASE WHEN co2_g_per_kwh IS NOT NULL
                                THEN COALESCE(p1, 0) + COALESCE(p2, 0)
                                END) AS co2_psum,
                           SUM(CASE WHEN price_per_kwh IS NOT NULL
                                THEN price_per_kwh * (COALESCE(p1, 0) + COALESCE(p2, 0))
                                END) AS price_wsum,
                           SUM(CASE WHEN price_per_kwh IS NOT NULL
                                THEN COALESCE(p1, 0) + COALESCE(p2, 0)
                                END) AS price_psum
                       FROM measurements
                       WHERE timestamp >= ?
                         AND online = 1
                         AND (COALESCE(p1, 0) + COALESCE(p2, 0)) > 0
                       GROUP BY day
                   )
                   SELECT
                       COALESCE(SUM(CASE WHEN co2_psum > 0
                                    THEN day_kwh END), 0) AS co2_kwh,
                       COALESCE(SUM(CASE WHEN co2_psum > 0
                                    THEN day_kwh * (co2_wsum / co2_psum) END), 0) AS co2_g,
                       COALESCE(SUM(CASE WHEN price_psum > 0
                                    THEN day_kwh END), 0) AS price_kwh,
                       COALESCE(SUM(CASE WHEN price_psum > 0
                                    THEN day_kwh * (price_wsum / price_psum) END), 0) AS price_sum
                   FROM days""",
                (month_start_ts,),
            )
            live = await cur2.fetchone()

            return {
                "co2_kwh":   float(agg["co2_kwh"])   + float(live["co2_kwh"]),
                "co2_g":     float(agg["co2_g"])     + float(live["co2_g"]),
                "price_kwh": float(agg["price_kwh"]) + float(live["price_kwh"]),
                "price_sum": float(agg["price_sum"]) + float(live["price_sum"]),
            }
