"""Tests for the v1.9 history additions: ISO-week helpers and the
daily_aggregates-backed weekly / daily-series / range-summary queries.

Pure ISO-week math needs no DB. The query tests use a temp SQLite database
seeded with measurements and backfilled into daily_aggregates, like
test_aggregates.py.
"""
import os
import tempfile
from datetime import datetime, date as D

import pytest
import pytest_asyncio

from app.database import Database
from app.date_helpers import iso_week_monday, iso_week_of


class TestIsoWeekHelpers:

    def test_monday_roundtrips(self):
        # iso_week_monday must return the Monday (isoweekday 1) of exactly that
        # ISO year/week — including 53-week years.
        for y, w in [(2026, 1), (2025, 52), (2024, 1), (2020, 53), (2026, 28)]:
            iy, iw, iwd = iso_week_monday(y, w).isocalendar()
            assert (iy, iw, iwd) == (y, w, 1)

    def test_iso_week_of(self):
        # Jan 1 2026 (a Thursday) is in ISO week 1 of 2026...
        assert iso_week_of(D(2026, 1, 1)) == (2026, 1)
        # ...and so is the preceding Monday, 2025-12-29.
        assert iso_week_of(datetime(2025, 12, 29)) == (2026, 1)


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest_asyncio.fixture
async def db(db_path):
    database = Database(db_path)
    await database.init()
    return database


async def _seed(db: Database, date_str: str, kwh: float, peak: int = 300) -> None:
    ts = int(datetime.strptime(date_str, "%Y-%m-%d").replace(hour=20).timestamp())
    half = kwh / 2.0
    await db.insert_measurement(
        ts, p1=peak // 2, p2=peak // 2, e1=half, e2=half,
        te1=half * 10, te2=half * 10, online=True, co2_g_per_kwh=400,
    )


class TestHistoryQueries:

    async def test_daily_series(self, db):
        for d, k in [("2026-05-01", 3), ("2026-05-02", 5), ("2026-05-03", 4)]:
            await _seed(db, d, k)
        await db.backfill_daily_aggregates()
        rows = await db.get_daily_series("2026-05-01", "2026-05-02")
        assert [r["date"] for r in rows] == ["2026-05-01", "2026-05-02"]
        assert rows[1]["kwh"] == pytest.approx(5.0)

    async def test_weekly_totals_group_by_iso_week(self, db):
        for d, k in [("2026-05-12", 2), ("2026-05-13", 3), ("2026-05-20", 4)]:
            await _seed(db, d, k)
        await db.backfill_daily_aggregates()
        weeks = await db.get_weekly_totals("2026-05-01", "2026-05-31")
        by = {(w["iso_year"], w["iso_week"]): w for w in weeks}
        wa = D.fromisoformat("2026-05-12").isocalendar()[:2]
        wb = D.fromisoformat("2026-05-20").isocalendar()[:2]
        assert wa != wb
        assert by[wa]["kwh"] == pytest.approx(5.0)   # 12th + 13th, same week
        assert by[wa]["days"] == 2
        assert by[wb]["kwh"] == pytest.approx(4.0)
        # week_start is always a Monday
        assert D.fromisoformat(by[wa]["week_start"]).isoweekday() == 1

    async def test_range_summary(self, db):
        for d, k in [("2026-05-01", 3), ("2026-05-02", 8), ("2026-05-03", 4)]:
            await _seed(db, d, k)
        await db.backfill_daily_aggregates()
        s = await db.get_range_summary("2026-05-01", "2026-05-03")
        assert s["days"] == 3
        assert s["total_kwh"] == pytest.approx(15.0)
        assert s["avg_per_day"] == pytest.approx(5.0)
        assert s["best_date"] == "2026-05-02"
        assert s["best_kwh"] == pytest.approx(8.0)

    async def test_range_summary_empty(self, db):
        s = await db.get_range_summary("2020-01-01", "2020-01-31")
        assert s["days"] == 0
        assert s["total_kwh"] == 0.0
        assert s["avg_per_day"] == 0.0
        assert s["best_date"] is None
        assert s["best_kwh"] is None


class TestFirstDailyDate:
    # get_first_daily_date is the lower bound for the drill-down period
    # navigation (v1.11.1). It must return the earliest daily_aggregates
    # date, and None on an empty database (which leaves the client's prev
    # arrow unbounded).

    async def test_returns_none_on_empty_db(self, db):
        assert await db.get_first_daily_date() is None

    async def test_returns_earliest_date(self, db):
        # Seeded out of order on purpose — MIN must not depend on insert order.
        for d, k in [("2026-05-02", 5), ("2026-04-30", 3), ("2026-05-01", 4)]:
            await _seed(db, d, k)
        await db.backfill_daily_aggregates()
        assert await db.get_first_daily_date() == "2026-04-30"
