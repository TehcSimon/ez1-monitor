"""Tests for the v1.8 money-realism and amortization features.

Two parts:
  - compute_money_saved: a pure helper for the self-consumption / feed-in
    money model (no DB needed).
  - Database.get_breakeven_date: an integration test over a temp SQLite DB
    seeded with daily aggregates, like test_aggregates.py.

compute_money_saved lives in app.money (a pure module, no FastAPI/inverter
imports) so this test needs neither INVERTER_IP nor the apsystems-ez1 library.
"""
import os
import tempfile
from datetime import datetime

import pytest
import pytest_asyncio

from app.database import Database
from app.money import compute_money_saved


# --- Pure money model -----------------------------------------------------

class TestComputeMoneySaved:

    def test_full_self_consumption_is_unchanged(self):
        # scq = 100% reproduces the 100%-self-consumption figure exactly,
        # so the default config matches pre-v1.8 behaviour...
        assert compute_money_saved(123.45, 1000.0, 100, 0.0) == pytest.approx(123.45)
        # ...and the feed-in tariff is irrelevant at 100% (nothing is fed in).
        assert compute_money_saved(123.45, 1000.0, 100, 0.08) == pytest.approx(123.45)

    def test_partial_self_consumption_without_feed_in(self):
        # 70% self-use, no feed-in tariff → 70% of the full figure.
        assert compute_money_saved(100.0, 500.0, 70, 0.0) == pytest.approx(70.0)

    def test_partial_self_consumption_with_feed_in(self):
        # 70% of 100 € self-used + 30% of 500 kWh fed in at 0.08 €/kWh.
        expected = 0.7 * 100.0 + 0.3 * 0.08 * 500.0
        assert compute_money_saved(100.0, 500.0, 70, 0.08) == pytest.approx(expected)

    def test_zero_self_consumption_is_feed_in_only(self):
        # 0% self-use → only the feed-in revenue remains.
        assert compute_money_saved(100.0, 500.0, 0, 0.08) == pytest.approx(0.08 * 500.0)

    def test_percentage_is_clamped(self):
        # Out-of-range percentages are clamped to [0, 100].
        assert compute_money_saved(100.0, 500.0, 150, 0.0) == pytest.approx(100.0)
        assert compute_money_saved(100.0, 500.0, -10, 0.08) == pytest.approx(0.08 * 500.0)


# --- Break-even date (integration) ----------------------------------------

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


def _ts(date_str: str, hour: int = 20) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hour).timestamp())


async def _seed_day(db: Database, date_str: str, kwh: float) -> None:
    """One end-of-day measurement so backfill records `kwh` for the day."""
    half = kwh / 2.0
    await db.insert_measurement(
        _ts(date_str), p1=100, p2=100, e1=half, e2=half,
        te1=half * 10, te2=half * 10, online=True, co2_g_per_kwh=400,
    )


class TestBreakevenDate:
    # Signature is get_breakeven_date(install_cost, total_savings): the day is
    # found at the install_cost/total_savings fraction of cumulative energy.

    async def test_returns_proportional_crossover_day(self, db):
        # Four equal days → cumulative energy fractions 0.25, 0.5, 0.75, 1.0.
        for day in ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"):
            await _seed_day(db, day, 5.0)
        await db.backfill_daily_aggregates()
        # cost is 50% of total savings → day at 50% cumulative energy (day 2).
        assert await db.get_breakeven_date(50.0, 100.0) == "2024-01-02"
        # 25% → day 1; 100% → the last day.
        assert await db.get_breakeven_date(25.0, 100.0) == "2024-01-01"
        assert await db.get_breakeven_date(100.0, 100.0) == "2024-01-04"

    async def test_none_when_not_amortized(self, db):
        await _seed_day(db, "2024-01-01", 5.0)
        await db.backfill_daily_aggregates()
        # cost exceeds savings (fraction > 1) → not broken even yet.
        assert await db.get_breakeven_date(150.0, 100.0) is None

    async def test_none_when_savings_zero(self, db):
        await _seed_day(db, "2024-01-01", 5.0)
        await db.backfill_daily_aggregates()
        assert await db.get_breakeven_date(50.0, 0.0) is None

    async def test_none_when_cost_zero(self, db):
        await _seed_day(db, "2024-01-01", 5.0)
        await db.backfill_daily_aggregates()
        assert await db.get_breakeven_date(0.0, 100.0) is None
