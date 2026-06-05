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
    online        INTEGER
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
    year            INTEGER NOT NULL,
    month           INTEGER NOT NULL,
    total_kwh       REAL NOT NULL DEFAULT 0,
    peak_w          INTEGER NOT NULL DEFAULT 0,
    days_with_data  INTEGER NOT NULL DEFAULT 0,
    last_updated    INTEGER NOT NULL,
    PRIMARY KEY (year, month)
);

CREATE TABLE IF NOT EXISTS yearly_aggregates (
    year            INTEGER PRIMARY KEY,
    total_kwh       REAL NOT NULL DEFAULT 0,
    peak_w          INTEGER NOT NULL DEFAULT 0,
    days_with_data  INTEGER NOT NULL DEFAULT 0,
    last_updated    INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(SCHEMA)
            await db.commit()

    async def insert_measurement(self, timestamp, p1, p2, e1, e2, te1, te2, online):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO measurements
                   (timestamp, p1, p2, e1, e2, te1, te2, online)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, p1, p2, e1, e2, te1, te2, 1 if online else 0),
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
            now_ts = int(datetime.now().timestamp())

            await db.execute(
                """INSERT OR REPLACE INTO monthly_aggregates
                   (year, month, total_kwh, peak_w, days_with_data, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (year, month, round(total_kwh, 3), int(peak_w), days_with_data, now_ts),
            )
            await db.commit()

            return {
                "year": year,
                "month": month,
                "total_kwh": round(total_kwh, 3),
                "peak_w": int(peak_w),
                "days_with_data": days_with_data,
            }

    async def recompute_year_aggregate(self, year: int) -> dict:
        """Recompute and upsert the yearly aggregate from its monthly rows.

        Should be called after recompute_month_aggregate() for any month
        in the same year.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT
                      COALESCE(SUM(total_kwh), 0)       AS total_kwh,
                      COALESCE(MAX(peak_w), 0)          AS peak_w,
                      COALESCE(SUM(days_with_data), 0)  AS days_with_data
                   FROM monthly_aggregates
                   WHERE year = ?""",
                (year,),
            )
            row = await cur.fetchone()

            total_kwh = row["total_kwh"] if row else 0.0
            peak_w = row["peak_w"] if row else 0
            days_with_data = row["days_with_data"] if row else 0
            now_ts = int(datetime.now().timestamp())

            await db.execute(
                """INSERT OR REPLACE INTO yearly_aggregates
                   (year, total_kwh, peak_w, days_with_data, last_updated)
                   VALUES (?, ?, ?, ?, ?)""",
                (year, round(total_kwh, 3), int(peak_w), days_with_data, now_ts),
            )
            await db.commit()

            return {
                "year": year,
                "total_kwh": round(total_kwh, 3),
                "peak_w": int(peak_w),
                "days_with_data": days_with_data,
            }

    async def get_monthly_aggregates(self, year: Optional[int] = None) -> list[dict]:
        """Read monthly_aggregates, optionally filtered to a single year."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if year is not None:
                cur = await db.execute(
                    """SELECT year, month, total_kwh, peak_w, days_with_data
                       FROM monthly_aggregates
                       WHERE year = ?
                       ORDER BY month ASC""",
                    (year,),
                )
            else:
                cur = await db.execute(
                    """SELECT year, month, total_kwh, peak_w, days_with_data
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
                """SELECT year, total_kwh, peak_w, days_with_data
                   FROM yearly_aggregates
                   ORDER BY year ASC"""
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
