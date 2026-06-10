"""Tests for the daily aggregates and Hall of Fame queries.

Unlike test_date_helpers.py these are integration tests that actually
spin up a SQLite database (in a temporary file), seed it with known
measurement data, and verify the aggregation queries return the
expected records.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from app.database import Database


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


@pytest.fixture
def db(db_path):
    """Initialized Database instance."""
    db = Database(db_path)
    asyncio.get_event_loop().run_until_complete(db.init())
    return db


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

    def test_backfill_empty_db(self, db):
        rows = asyncio.get_event_loop().run_until_complete(
            db.backfill_daily_aggregates()
        )
        assert rows == 0

    def test_backfill_single_day(self, db):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(_seed_day(db, "2025-06-15", 3.5, 2.1))
        rows = loop.run_until_complete(db.backfill_daily_aggregates())
        assert rows == 1
        best = loop.run_until_complete(db.get_best_day())
        assert best is not None
        assert best["date"] == "2025-06-15"
        assert abs(best["total_kwh"] - 5.6) < 0.01

    def test_best_day_picks_highest(self, db):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(_seed_day(db, "2025-06-15", 3.0, 2.0))
        loop.run_until_complete(_seed_day(db, "2025-06-16", 4.5, 3.0))
        loop.run_until_complete(_seed_day(db, "2025-06-17", 2.0, 1.5))
        loop.run_until_complete(db.backfill_daily_aggregates())
        best = loop.run_until_complete(db.get_best_day())
        assert best["date"] == "2025-06-16"
        assert abs(best["total_kwh"] - 7.5) < 0.01

    def test_best_day_in_range_respects_bounds(self, db):
        loop = asyncio.get_event_loop()
        # 10 kWh on Jun 1, 5 kWh on Jul 1
        loop.run_until_complete(_seed_day(db, "2025-06-01", 6.0, 4.0))
        loop.run_until_complete(_seed_day(db, "2025-07-01", 3.0, 2.0))
        loop.run_until_complete(db.backfill_daily_aggregates())
        # June only
        june = loop.run_until_complete(
            db.get_best_day_in_range("2025-06-01", "2025-06-30")
        )
        assert june["date"] == "2025-06-01"
        # July only
        july = loop.run_until_complete(
            db.get_best_day_in_range("2025-07-01", "2025-07-31")
        )
        assert july["date"] == "2025-07-01"
        # No-data window
        empty = loop.run_until_complete(
            db.get_best_day_in_range("2024-01-01", "2024-12-31")
        )
        assert empty is None

    def test_data_extent_counts_completed_periods(self, db):
        loop = asyncio.get_event_loop()
        # Three days in week 25 of 2025 (Jun 16-18), three in week 26 (Jun 23-25)
        for d in ["2025-06-16", "2025-06-17", "2025-06-18",
                  "2025-06-23", "2025-06-24", "2025-06-25"]:
            loop.run_until_complete(_seed_day(db, d, 2.0, 1.5))
        loop.run_until_complete(db.backfill_daily_aggregates())
        extent = loop.run_until_complete(db.get_data_extent())
        assert extent["days_with_data"] == 6
        assert extent["first_date"] == "2025-06-16"
        assert extent["last_date"] == "2025-06-25"
        # Whether weeks are "completed" depends on current date; just verify
        # the field exists and is non-negative
        assert extent["completed_weeks"] >= 0
        assert extent["completed_months"] >= 0


class TestBestWeek:

    def test_groups_days_by_iso_week(self, db):
        loop = asyncio.get_event_loop()
        # Week 25 of 2025 = Mon Jun 16 to Sun Jun 22
        loop.run_until_complete(_seed_day(db, "2025-06-16", 3.0, 2.0))  # 5 kWh
        loop.run_until_complete(_seed_day(db, "2025-06-17", 4.0, 3.0))  # 7 kWh
        loop.run_until_complete(_seed_day(db, "2025-06-18", 2.5, 2.0))  # 4.5 kWh
        # Week 25 total: 16.5
        # Week 26 of 2025 = Mon Jun 23 to Sun Jun 29
        loop.run_until_complete(_seed_day(db, "2025-06-23", 5.0, 4.0))  # 9 kWh
        loop.run_until_complete(_seed_day(db, "2025-06-24", 4.0, 3.0))  # 7 kWh
        loop.run_until_complete(_seed_day(db, "2025-06-25", 3.0, 2.0))  # 5 kWh
        # Week 26 total: 21
        loop.run_until_complete(db.backfill_daily_aggregates())

        best = loop.run_until_complete(db.get_best_week())
        assert best is not None
        assert best["iso_year"] == 2025
        assert best["iso_week"] == 26
        assert abs(best["total_kwh"] - 21.0) < 0.01
        assert best["week_start"] == "2025-06-23"  # Monday

    def test_returns_none_when_empty(self, db):
        loop = asyncio.get_event_loop()
        best = loop.run_until_complete(db.get_best_week())
        assert best is None


class TestProductionWindow:

    def test_window_empty_when_no_data(self, db):
        loop = asyncio.get_event_loop()
        first, last = loop.run_until_complete(db.get_today_production_window())
        assert first is None
        assert last is None
