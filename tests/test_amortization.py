"""Tests for the v1.8 money-realism and amortization features.

Three parts:
  - compute_money_saved: a pure helper for the self-consumption / feed-in
    money model (no DB needed).
  - Database.get_breakeven_date: an integration test over a temp SQLite DB
    seeded with daily aggregates, like test_aggregates.py.
  - Tariff changes: what stays and what moves when PRICE_PER_KWH is edited,
    over a temp SQLite DB seeded the way the poller stamps its readings.

compute_money_saved lives in app.money (a pure module, no FastAPI/inverter
imports) so this test needs neither INVERTER_IP nor the apsystems-ez1 library.
"""
import os
import tempfile
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio

from app.database import Database
from app.money import (
    average_price_per_kwh, compute_money_saved, compute_money_saved_full,
    estimate_breakeven_date, value_of_new_kwh,
)


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


class TestComputeMoneySavedFull:

    def test_stamped_energy_keeps_its_value(self):
        # Everything stamped: neither fallback price plays a role.
        assert compute_money_saved_full(35.0, 100.0, 100.0, 0.35, 0.28) == pytest.approx(35.0)

    def test_remainder_is_valued_at_the_oldest_price(self):
        # 100 kWh stamped, 400 kWh on the counter without a stamp.
        assert compute_money_saved_full(35.0, 100.0, 500.0, 0.35, 0.28) == pytest.approx(175.0)

    def test_fresh_install_falls_back_to_the_current_price(self):
        # No stamped month yet, so there is no oldest price either.
        assert compute_money_saved_full(0.0, 0.0, 500.0, None, 0.28) == pytest.approx(140.0)

    def test_oldest_price_of_zero_is_a_price(self):
        # PRICE_PER_KWH=0 is a legitimate setting; 0 must not read as "unset".
        assert compute_money_saved_full(0.0, 100.0, 500.0, 0.0, 0.28) == pytest.approx(0.0)

    def test_recorded_energy_beyond_the_counter_adds_nothing(self):
        # Rounding can put the daily sums slightly above the lifetime counter.
        assert compute_money_saved_full(35.0, 100.0, 99.0, 0.35, 0.28) == pytest.approx(35.0)


class TestAveragePricePerKwh:

    def test_one_tariff_averages_to_that_tariff(self):
        assert average_price_per_kwh(175.0, 500.0, 100.0) == pytest.approx(0.35)

    def test_two_tariffs_are_weighted_by_energy(self):
        # 300 kWh at 35 ct and 100 kWh at 28 ct.
        full = 300 * 0.35 + 100 * 0.28
        assert average_price_per_kwh(full, 400.0, 400.0) == pytest.approx(0.3325)

    def test_stamped_energy_beyond_the_counter_is_the_basis(self):
        # Same case as in compute_money_saved_full: nothing may exceed the
        # stamped price just because the counter rounded below the daily sums.
        assert average_price_per_kwh(35.0, 99.0, 100.0) == pytest.approx(0.35)

    def test_no_energy_has_no_average(self):
        assert average_price_per_kwh(0.0, 0.0, 0.0) is None


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

    async def test_pre_monitor_energy_counts_as_accrued(self, db):
        # 20 kWh recorded (4 × 5), but the lifetime counter says 40: 20 kWh
        # were produced before the monitor was set up.
        for day in ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"):
            await _seed_day(db, day, 5.0)
        await db.backfill_daily_aggregates()
        # Cost = 75 % of savings → crossover at 30 of 40 kWh = 10 recorded
        # kWh in → day 2. Ignoring the pre-monitor share said day 3.
        assert await db.get_breakeven_date(75.0, 100.0, lifetime_kwh=40.0) == "2024-01-02"
        assert await db.get_breakeven_date(75.0, 100.0) == "2024-01-03"

    async def test_none_when_paid_off_before_monitoring(self, db):
        for day in ("2024-01-01", "2024-01-02"):
            await _seed_day(db, day, 5.0)
        await db.backfill_daily_aggregates()
        # 90 of 100 kWh predate the monitor; cost = 50 % of savings was
        # reached before the first recorded day → no datable crossover.
        assert await db.get_breakeven_date(50.0, 100.0, lifetime_kwh=100.0) is None


# --- Projected break-even date (v1.10) --------------------------------------

class TestEstimateBreakevenDate:
    """Pure linear ETA over the whole history, gated behind a full year of
    data so the rate isn't seasonally biased."""

    TODAY = date(2026, 7, 7)

    def test_linear_extrapolation(self):
        # 1000 kWh over exactly 2 years at 0.40/kWh → rate = 400/730 per day.
        # Remaining 400 → another 730 days.
        first = self.TODAY - timedelta(days=730)
        eta = estimate_breakeven_date(800.0, 400.0, first, self.TODAY, 1000.0, 0.40)
        assert eta == self.TODAY + timedelta(days=730)

    def test_remaining_days_round_up(self):
        # 1000 kWh over 400 days at 0.30/kWh → rate 0.75/day; remaining 100 →
        # 133.33 days, rounded UP to 134 (never promise an earlier date).
        first = self.TODAY - timedelta(days=400)
        eta = estimate_breakeven_date(400.0, 300.0, first, self.TODAY, 1000.0, 0.30)
        assert eta == self.TODAY + timedelta(days=134)

    def test_gated_below_one_year_of_data(self):
        # 364 days of history → seasonally biased, no estimate.
        first = self.TODAY - timedelta(days=364)
        assert estimate_breakeven_date(800.0, 400.0, first, self.TODAY, 1000.0, 0.40) is None
        # Exactly 365 days → estimate appears.
        first = self.TODAY - timedelta(days=365)
        assert estimate_breakeven_date(800.0, 400.0, first, self.TODAY, 1000.0, 0.40) is not None

    def test_none_when_already_amortized(self):
        # The real break-even date (get_breakeven_date) takes over here.
        first = self.TODAY - timedelta(days=800)
        assert estimate_breakeven_date(800.0, 800.0, first, self.TODAY, 1000.0, 0.40) is None
        assert estimate_breakeven_date(800.0, 900.0, first, self.TODAY, 1000.0, 0.40) is None

    def test_none_without_cost_or_savings(self):
        first = self.TODAY - timedelta(days=800)
        assert estimate_breakeven_date(0.0, 100.0, first, self.TODAY, 1000.0, 0.40) is None
        assert estimate_breakeven_date(800.0, 0.0, first, self.TODAY, 1000.0, 0.40) is None

    def test_rate_ignores_savings_from_before_the_recorded_span(self):
        # 400 saved in total, but 200 of it predates the monitor (lifetime
        # counter). The 365 recorded days only hold 500 kWh = 200 at 0.40/kWh
        # → the remaining 400 take 730 days, not 365.
        first = self.TODAY - timedelta(days=365)
        eta = estimate_breakeven_date(800.0, 400.0, first, self.TODAY, 500.0, 0.40)
        assert eta == self.TODAY + timedelta(days=730)

    def test_future_production_is_valued_at_the_current_tariff(self):
        # Regression guard: after a tariff change, the date used to keep
        # following the average of everything saved so far. Same savings and
        # same production at half the price → twice the remaining time.
        first = self.TODAY - timedelta(days=730)
        at_old = estimate_breakeven_date(800.0, 400.0, first, self.TODAY, 1000.0, 0.40)
        at_new = estimate_breakeven_date(800.0, 400.0, first, self.TODAY, 1000.0, 0.20)
        assert at_old == self.TODAY + timedelta(days=730)
        assert at_new == self.TODAY + timedelta(days=1460)

    def test_no_savings_rate_gives_no_estimate(self):
        first = self.TODAY - timedelta(days=400)
        # Nothing recorded (e.g. only unusable days), or kWh worth nothing.
        assert estimate_breakeven_date(800.0, 400.0, first, self.TODAY, 0.0, 0.40) is None
        assert estimate_breakeven_date(800.0, 400.0, first, self.TODAY, 1000.0, 0.0) is None


class TestValueOfNewKwh:

    def test_full_self_consumption_is_the_retail_price(self):
        assert value_of_new_kwh(0.28, 100, 0.08) == pytest.approx(0.28)

    def test_partial_self_consumption_weighs_in_the_feed_in_tariff(self):
        assert value_of_new_kwh(0.28, 70, 0.08) == pytest.approx(0.7 * 0.28 + 0.3 * 0.08)


# --- Tariff changes (integration) -------------------------------------------

class TestTariffChange:
    """Editing PRICE_PER_KWH must only affect new production. Readings carry
    the price stamped by the poller, and energy on the lifetime counter
    without a stamp stays at the oldest stamped price. Regression guard: that
    remainder used to follow the current price, so the money and payback
    cards jumped on every tariff change although nothing had been produced."""

    OLD, NEW = 0.35, 0.28

    async def _seed(self, db: Database, day: date, kwh: float, price, lifetime: float) -> None:
        """One reading whose power is proportional to the day's energy, so
        the energy-weighted monthly price comes out exact."""
        half = kwh / 2.0
        ts = int(datetime(day.year, day.month, day.day, 12).timestamp())
        await db.insert_measurement(
            ts, p1=half * 100, p2=half * 100, e1=half, e2=half,
            te1=lifetime / 2.0, te2=lifetime / 2.0, online=True, price_per_kwh=price,
        )

    async def _money(self, db: Database, current_price: float) -> float:
        """The lifetime figure of /api/stats at the given PRICE_PER_KWH.
        August 2024 is the "running" month of these tests."""
        split = await db.get_lifetime_factor_split(2024, 8)
        return compute_money_saved_full(
            split["price_sum"], split["price_kwh"], await db.get_total_energy(),
            split["oldest_price"], current_price,
        )

    async def test_editing_the_price_moves_nothing_already_produced(self, db):
        # 400 kWh on the counter from before the monitor, 30 kWh recorded.
        lifetime = 400.0
        for d in range(1, 31):
            lifetime += 1.0
            await self._seed(db, date(2024, 6, d), 1.0, self.OLD, lifetime)
        await db.recompute_month_aggregate(2024, 6)
        expected = 430 * self.OLD
        assert await self._money(db, self.OLD) == pytest.approx(expected)
        assert await self._money(db, self.NEW) == pytest.approx(expected)

    async def test_new_production_is_valued_at_the_new_price(self, db):
        await self._seed(db, date(2024, 6, 1), 10.0, self.OLD, 410.0)
        await db.recompute_month_aggregate(2024, 6)
        before = await self._money(db, self.NEW)
        # The running month is read live from the raw rows.
        await self._seed(db, date(2024, 8, 1), 3.0, self.NEW, 413.0)
        assert await self._money(db, self.NEW) == pytest.approx(before + 3 * self.NEW)

    async def test_switch_inside_a_completed_month_is_exact(self, db):
        lifetime, expected = 0.0, 0.0
        for d in range(1, 31):
            # Unequal days, so a plain average of the two prices would be off.
            kwh = 5.0 if d % 2 else 1.0
            price = self.OLD if d <= 20 else self.NEW
            lifetime += kwh
            expected += kwh * price
            await self._seed(db, date(2024, 6, d), kwh, price, lifetime)
        await db.recompute_month_aggregate(2024, 6)
        assert await self._money(db, self.NEW) == pytest.approx(expected, abs=0.01)

    async def test_pruned_months_keep_their_price(self, db):
        await self._seed(db, date(2024, 5, 1), 10.0, self.OLD, 10.0)
        await db.recompute_month_aggregate(2024, 5)
        before = await self._money(db, self.NEW)
        # Keep a current lifetime reading, as a running poller would.
        now = datetime.now()
        await db.insert_measurement(
            int(now.timestamp()), p1=0, p2=0, e1=0, e2=0, te1=5.0, te2=5.0,
            online=True, price_per_kwh=self.NEW,
        )
        assert await db.delete_old_measurements(1) == 1
        assert await self._money(db, self.NEW) == pytest.approx(before)

    async def test_unstamped_month_counts_as_remainder(self, db):
        # A month from before stamping existed (v1.6.1), then a stamped one:
        # the old month is valued at the earliest price actually known.
        await self._seed(db, date(2024, 5, 1), 10.0, None, 10.0)
        await db.recompute_month_aggregate(2024, 5)
        await self._seed(db, date(2024, 6, 1), 10.0, self.OLD, 20.0)
        await db.recompute_month_aggregate(2024, 6)
        split = await db.get_lifetime_factor_split(2024, 8)
        assert split["price_kwh"] == pytest.approx(10.0)
        assert split["oldest_price"] == pytest.approx(self.OLD)
        assert await self._money(db, self.NEW) == pytest.approx(20 * self.OLD)

    async def test_oldest_price_ignores_later_tariffs(self, db):
        await self._seed(db, date(2024, 6, 1), 10.0, self.NEW, 10.0)
        await self._seed(db, date(2024, 5, 1), 10.0, self.OLD, 20.0)
        await db.recompute_month_aggregate(2024, 6)
        await db.recompute_month_aggregate(2024, 5)
        split = await db.get_lifetime_factor_split(2024, 8)
        assert split["oldest_price"] == pytest.approx(self.OLD)

    async def test_no_stamped_month_reports_no_oldest_price(self, db):
        split = await db.get_lifetime_factor_split(2024, 8)
        assert split["oldest_price"] is None
