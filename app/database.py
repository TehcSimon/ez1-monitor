"""SQLite database layer for EZ1 measurements."""
import aiosqlite
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    timestamp     INTEGER PRIMARY KEY,
    p1            REAL,       -- Power channel 1 (W)
    p2            REAL,       -- Power channel 2 (W)
    e1            REAL,       -- Energy today channel 1 (kWh)
    e2            REAL,       -- Energy today channel 2 (kWh)
    te1           REAL,       -- Total energy channel 1 (kWh)
    te2           REAL,       -- Total energy channel 2 (kWh)
    online        INTEGER     -- 1 = poll ok, 0 = poll failed
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

    async def insert_measurement(
        self,
        timestamp: int,
        p1: Optional[float],
        p2: Optional[float],
        e1: Optional[float],
        e2: Optional[float],
        te1: Optional[float],
        te2: Optional[float],
        online: bool,
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO measurements
                   (timestamp, p1, p2, e1, e2, te1, te2, online)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, p1, p2, e1, e2, te1, te2, 1 if online else 0),
            )
            await db.commit()

    async def update_device_info(
        self,
        device_id: str,
        serial_number: str,
        min_power: int,
        max_power: int,
    ):
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

    async def get_range(self, start_ts: int, end_ts: int, bucket_seconds: int = 0) -> list[dict]:
        """Get measurements in time range. If bucket_seconds > 0, aggregate."""
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
                # Bucket aggregation: average power, max energy-today (cumulative)
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
        """Get daily kWh totals for the last N days (based on max e1+e2 per day)."""
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

    async def get_total_energy(self) -> float:
        """Get total lifetime energy (latest te1+te2)."""
        latest = await self.get_latest()
        if not latest:
            return 0.0
        return (latest.get("te1") or 0) + (latest.get("te2") or 0)
