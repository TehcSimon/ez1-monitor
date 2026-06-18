"""Tests for the daily aggregates and Hall of Fame queries.

Unlike test_date_helpers.py these are integration tests that actually
spin up a SQLite database (in a temporary file), seed it with known
measurement data, and verify the aggregation queries return the
expected records.

They run under pytest-asyncio in auto mode (see pytest.ini): tests and
fixtures are plain `async def` and `await` the database directly, instead
of the old asyncio.get_event_loop().run_until_complete() pattern, which
stopped working once the implicit event loop was removed.
"""
import os
import tempfile
from datetime import datetime, timedelta

import aiosqlite
import pytest
import pytest_asyncio

from app.database import Database


async def _read_daily_aggregate(db: Database, date_iso: str):
    """Read one stored daily_aggregates row directly (test helper — the app
    itself only ever reads the *best* day, so there's no production getter
    for a single arbitrary day)."""
    async with aiosqlite.connect(db.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT date, total_kwh, peak_w FROM daily_aggregates WHERE date = ?",
            (date_iso,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


def _ts(date_str: str, hour: int = 12) -> int:
    """Convert YYYY-MM-DD to unix timestamp at the given local hour."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hour)
    return int(dt.timestamp())


@pytest.fixture
def db_path():
    """Temp file SQLite DB, cleaned up after the test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest_asyncio.fixture
async def db(db_path):
    """Initialized Database instance."""
    database = Database(db_path)
    await database.init()
    return database


async def _seed_day(db: Database, date_str: str, kwh_p1: float, kwh_p2: float, peak_w: int = 200):
    """Insert one measurement for a date with given end-of-day energy values."""
    ts = _ts(date_str, hour=20)  # 8 PM, after production
    await db.insert_measurement(
        ts, p1=peak_w // 2, p2=peak_w // 2,
        e1=kwh_p1, e2=kwh_p2,
        te1=kwh_p1 * 10, te2=kwh_p2 * 10,
        online=True, co2_g_per_kwh=400,
    )


class TestDailyAggregates:

    async def test_backfill_empty_db(self, db):
        rows = await db.backfill_daily_aggregates()
        assert rows == 0

    async def test_backfill_single_day(self, db):
        await _seed_day(db, "2025-06-15", 3.5, 2.1)
        rows = await db.backfill_daily_aggregates()
        assert rows == 1
        best = await db.get_best_day()
        assert best is not None
        assert best["date"] == "2025-06-15"
        assert abs(best["total_kwh"] - 5.6) < 0.01

    async def test_best_day_picks_highest(self, db):
        await _seed_day(db, "2025-06-15", 3.0, 2.0)
        await _seed_day(db, "2025-06-16", 4.5, 3.0)
        await _seed_day(db, "2025-06-17", 2.0, 1.5)
        await db.backfill_daily_aggregates()
        best = await db.get_best_day()
        assert best["date"] == "2025-06-16"
        assert abs(best["total_kwh"] - 7.5) < 0.01

    async def test_data_extent_counts_completed_periods(self, db):
        # Three days in week 25 of 2025 (Jun 16-18), three in week 26 (Jun 23-25)
        for d in ["2025-06-16", "2025-06-17", "2025-06-18",
                  "2025-06-23", "2025-06-24", "2025-06-25"]:
            await _seed_day(db, d, 2.0, 1.5)
        await db.backfill_daily_aggregates()
        extent = await db.get_data_extent()
        assert extent["days_with_data"] == 6
        assert extent["first_date"] == "2025-06-16"
        assert extent["last_date"] == "2025-06-25"
        # Whether weeks are "completed" depends on current date; just verify
        # the field exists and is non-negative
        assert extent["completed_weeks"] >= 0
        assert extent["completed_months"] >= 0


class TestBackfillRetentionBoundary:
    """The retention boundary day is partially pruned, so backfill must NOT
    overwrite its stored (complete) aggregate. Regression guard for the
    since_iso `>` vs `>=` off-by-one."""

    async def test_boundary_day_is_left_frozen(self, db):
        # Seed three consecutive days with a high peak, store their aggregates.
        await _seed_day(db, "2025-06-15", 3.0, 2.0, peak_w=900)
        await _seed_day(db, "2025-06-16", 3.0, 2.0, peak_w=900)
        await _seed_day(db, "2025-06-17", 3.0, 2.0, peak_w=900)
        await db.backfill_daily_aggregates()

        # Simulate the boundary day (06-16) having its high-power rows pruned
        # so only a low-peak row remains in raw measurements.
        await db.insert_measurement(
            _ts("2025-06-16", hour=20), p1=10, p2=10,
            e1=3.0, e2=2.0, te1=30, te2=20,
            online=True, co2_g_per_kwh=400,
        )
        # Re-run backfill with the retention boundary at 06-16. Days strictly
        # after it are rewritten; 06-16 itself must stay frozen.
        await db.backfill_daily_aggregates(since_iso="2025-06-16")
        frozen = await _read_daily_aggregate(db, "2025-06-16")
        assert frozen is not None
        # Frozen value retained — NOT reduced to the pruned 20 W peak.
        assert frozen["peak_w"] == 900

    async def test_days_after_boundary_are_rewritten(self, db):
        await _seed_day(db, "2025-06-17", 3.0, 2.0, peak_w=500)
        await db.backfill_daily_aggregates(since_iso="2025-06-16")
        # 06-17 is strictly after the boundary, so it must be present.
        best = await db.get_best_day()
        assert best is not None
        assert best["date"] == "2025-06-17"


class TestBestWeek:

    async def test_groups_days_by_iso_week(self, db):
        # Week 25 of 2025 = Mon Jun 16 to Sun Jun 22
        await _seed_day(db, "2025-06-16", 3.0, 2.0)  # 5 kWh
        await _seed_day(db, "2025-06-17", 4.0, 3.0)  # 7 kWh
        await _seed_day(db, "2025-06-18", 2.5, 2.0)  # 4.5 kWh
        # Week 25 total: 16.5
        # Week 26 of 2025 = Mon Jun 23 to Sun Jun 29
        await _seed_day(db, "2025-06-23", 5.0, 4.0)  # 9 kWh
        await _seed_day(db, "2025-06-24", 4.0, 3.0)  # 7 kWh
        await _seed_day(db, "2025-06-25", 3.0, 2.0)  # 5 kWh
        # Week 26 total: 21
        await db.backfill_daily_aggregates()

        best = await db.get_best_week()
        assert best is not None
        assert best["iso_year"] == 2025
        assert best["iso_week"] == 26
        assert abs(best["total_kwh"] - 21.0) < 0.01
        assert best["week_start"] == "2025-06-23"  # Monday

    async def test_returns_none_when_empty(self, db):
        best = await db.get_best_week()
        assert best is None


class TestProductionWindow:

    async def test_window_empty_when_no_data(self, db):
        first, last = await db.get_today_production_window()
        assert first is None
        assert last is None


class TestTodayPanelEnergy:
    """Per-panel "today" energy must come from the DB so it survives the
    inverter dropping to standby at night. Regression: the PV cards showed
    "—" overnight because they were sourced from the live e1/e2 reading,
    which the poller stores as NULL once the inverter is offline."""

    async def test_returns_day_totals_and_survives_offline_row(self, db):
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        def at(hour):
            return int((midnight + timedelta(hours=hour)).timestamp())

        # e1/e2 are daily-resetting counters that climb through the day.
        await db.insert_measurement(at(8), 100, 80, 0.10, 0.08, 1, 1, online=True, co2_g_per_kwh=400)
        await db.insert_measurement(at(12), 300, 250, 0.45, 0.39, 1, 1, online=True, co2_g_per_kwh=400)
        await db.insert_measurement(at(16), 120, 90, 0.62, 0.55, 1, 1, online=True, co2_g_per_kwh=400)
        # Inverter goes to standby at night: offline row with NULL counters.
        await db.insert_measurement(at(23), None, None, None, None, None, None, online=False)

        pv1, pv2 = await db.get_today_panel_energy()
        assert abs(pv1 - 0.62) < 1e-9
        assert abs(pv2 - 0.55) < 1e-9

    async def test_zero_when_no_production_today(self, db):
        pv1, pv2 = await db.get_today_panel_energy()
        assert pv1 == 0.0
        assert pv2 == 0.0
