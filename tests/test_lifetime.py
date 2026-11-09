"""Tests for the lifetime counter correction (app/lifetime.py) and the figures
built on it, against a real temp SQLite file."""
import importlib
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio

from app.database import Database
from app.lifetime import GLITCH_WINDOW, corrected_counter


class TestCorrectedCounter:
    def test_no_readings(self):
        assert corrected_counter([]) == 0.0

    @pytest.mark.parametrize("readings", [
        [5.0],
        [5.0, 5.0, 5.0],
        [1.0, 2.5, 2.5, 40.0, 41.0],
        # Whole weeks without a monitor: a big step up is still normal.
        [100.0, 300.0, 301.0],
        # Below the tolerance: rounding, not a wrap.
        [100.0, 99.5, 100.2],
    ])
    def test_a_counter_that_never_falls_is_its_latest_reading(self, readings):
        assert corrected_counter(readings) == readings[-1]

    def test_a_wrap_adds_the_old_level_back(self):
        assert corrected_counter([538.0, 539.8, 1.5, 3.0]) == pytest.approx(539.8 + 3.0)

    def test_two_wraps_add_up(self):
        readings = [530.0, 539.0, *range(1, 16), 530.0, 539.0, 2.0, 3.0]
        assert corrected_counter(readings) == pytest.approx(539.0 + 539.0 + 3.0)

    def test_a_lasting_partial_fall_keeps_the_total_where_it_was(self):
        # Regression guard: adding back the whole old level, as for an
        # overflow, would have counted the remaining 150 kWh twice.
        later = [150.0 + i * 0.2 for i in range(20)]
        assert corrected_counter([200.0, 201.0, *later]) == pytest.approx(201.0 + later[-1] - 150.0)

    def test_a_replaced_inverter_carries_on_from_the_old_total(self):
        later = [0.5 + i * 0.5 for i in range(20)]
        assert corrected_counter([199.0, 200.0, *later]) == pytest.approx(200.0 + later[-1] - 0.5)

    def test_a_small_fall_the_counter_soon_regrows_adds_nothing(self):
        assert corrected_counter([200.0, 201.0, 198.0, 199.0, 200.5, 202.0]) == 202.0

    def test_a_single_bad_latest_poll_changes_nothing(self):
        assert corrected_counter([146.0, 147.0, 0.0]) == 147.0

    def test_a_wrap_is_held_until_the_next_reading_confirms_it(self):
        day_of_the_wrap = [538.0, 539.8, 0.3]
        assert corrected_counter(day_of_the_wrap) == 539.8
        assert corrected_counter([*day_of_the_wrap[:2], 1.5, 1.5]) == pytest.approx(541.3)

    @pytest.mark.parametrize("glitch", [[0.0], [0.0, 0.0], [5.0, 0.0, 7.0]])
    def test_a_dip_that_recovers_is_no_wrap(self, glitch):
        assert corrected_counter([146.0, 147.0, *glitch, 148.0, 149.0]) == 149.0

    @pytest.mark.parametrize("spike", [[9999.0], [9999.0, 9999.0]])
    def test_a_spike_that_recovers_is_no_wrap(self, spike):
        assert corrected_counter([146.0, 147.0, *spike, 148.0, 149.0]) == 149.0

    def test_a_spike_as_the_very_first_reading_is_ignored(self):
        assert corrected_counter([9999.0, 148.0, 149.0]) == 149.0

    def test_a_counter_back_near_the_old_level_later_is_not_a_glitch(self):
        # Regrown to just under the level of the first wrap, long after it.
        regrown = [float(v) for v in range(30, 540, 30)]
        assert len(regrown) > GLITCH_WINDOW
        assert corrected_counter([538.0, 539.0, 2.0, *regrown]) == pytest.approx(539.0 + regrown[-1])


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.unlink(path + suffix)


@pytest_asyncio.fixture
async def db(db_path):
    database = Database(db_path)
    await database.init()
    return database


async def _reading(db, days_ago, te1, te2=10.0, hour=12):
    moment = (datetime.now() - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0)
    await db.insert_measurement(int(moment.timestamp()), 100, 100, 1.0, 1.0, te1, te2, online=True)


class TestTotalEnergy:
    async def test_no_data(self, db):
        assert await db.get_total_energy() == 0.0

    async def test_rising_counters_are_taken_as_they_are(self, db):
        for days_ago, te1 in ((3, 100.0), (2, 101.0), (1, 102.5)):
            await _reading(db, days_ago, te1)
        await db.backfill_daily_aggregates()
        assert await db.get_total_energy() == pytest.approx(112.5)

    async def test_a_wrap_keeps_the_total_after_the_raw_rows_are_gone(self, db, db_path):
        for days_ago, te1 in ((4, 538.0), (3, 539.8), (2, 1.5), (1, 3.0)):
            await _reading(db, days_ago, te1)
        await db.backfill_daily_aggregates()
        con = sqlite3.connect(db_path)
        cutoff = int((datetime.now() - timedelta(days=2, hours=12)).timestamp())
        con.execute("DELETE FROM measurements WHERE timestamp < ?", (cutoff,))
        con.commit()
        con.close()
        assert await db.get_total_energy() == pytest.approx(539.8 + 3.0 + 10.0)

    async def test_upgraded_days_without_counter_values_are_skipped(self, db, db_path):
        await _reading(db, 1, 50.0)
        con = sqlite3.connect(db_path)
        con.execute("INSERT INTO daily_aggregates (date, total_kwh, peak_w, last_updated)"
                    " VALUES ('2020-01-01', 1, 1, 0)")
        con.commit()
        con.close()
        assert await db.get_total_energy() == pytest.approx(60.0)

    async def test_the_recorded_energy_leaves_today_to_the_caller(self, db):
        assert await db.get_recorded_energy_before_today() == 0.0
        await _reading(db, 2, 100.0)
        await _reading(db, 1, 101.0)
        await _reading(db, 0, 102.0, hour=0)
        await db.backfill_daily_aggregates()
        assert await db.get_recorded_energy_before_today() == pytest.approx(4.0)

    async def test_the_recorded_energy_survives_pruning(self, db, db_path):
        await _reading(db, 2, 100.0)
        await db.backfill_daily_aggregates()
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM measurements")
        con.commit()
        con.close()
        assert await db.get_recorded_energy_before_today() == pytest.approx(2.0)

    async def test_migration_adds_the_new_columns_to_an_old_database(self, db_path):
        con = sqlite3.connect(db_path)
        con.executescript(
            """CREATE TABLE measurements (timestamp INTEGER PRIMARY KEY, p1 REAL, p2 REAL,
                   e1 REAL, e2 REAL, te1 REAL, te2 REAL, online INTEGER);
               CREATE TABLE daily_aggregates (date TEXT PRIMARY KEY,
                   total_kwh REAL NOT NULL DEFAULT 0, peak_w INTEGER NOT NULL DEFAULT 0,
                   last_updated INTEGER NOT NULL);"""
        )
        con.commit()
        con.close()
        database = Database(db_path)
        await database.init()
        await database.init()  # idempotent
        con = sqlite3.connect(db_path)
        measurement_cols = {r[1] for r in con.execute("PRAGMA table_info(measurements)")}
        daily_cols = {r[1] for r in con.execute("PRAGMA table_info(daily_aggregates)")}
        con.close()
        assert {"temp_c", "grid_v", "grid_hz"} <= measurement_cols
        assert {"te1", "te2"} <= daily_cols


@pytest_asyncio.fixture
async def main(db, db_path, monkeypatch):
    """app.main wired to the temp database. Importing it needs both variables."""
    monkeypatch.setenv("INVERTER_IP", "192.0.2.1")
    monkeypatch.setenv("DB_PATH", db_path)
    module = importlib.import_module("app.main")
    monkeypatch.setattr(module, "db", db)
    return module


class TestStatsEndpoint:
    async def test_recorded_energy_is_what_the_bars_add_up_to(self, main):
        await _reading(main.db, 2, 100.0)
        await _reading(main.db, 1, 101.0)
        await main.db.backfill_daily_aggregates()
        stats = await main.get_stats()
        # The inverter's counters also hold what came before the monitor.
        assert stats["total_kwh"] == pytest.approx(111.0)
        assert stats["recorded_kwh"] == pytest.approx(4.0)
        assert stats["first_data_date"] == (date.today() - timedelta(days=2)).isoformat()

    async def test_today_counts_before_the_hourly_refresh(self, main):
        await _reading(main.db, 0, 100.0, hour=0)
        stats = await main.get_stats()
        assert stats["recorded_kwh"] == pytest.approx(2.0)
        assert stats["first_data_date"] == date.today().isoformat()

    async def test_the_offset_moves_the_total_and_the_estimates_only(self, main, monkeypatch):
        await _reading(main.db, 1, 100.0)
        await main.db.backfill_daily_aggregates()
        before = await main.get_stats()
        monkeypatch.setattr(main, "LIFETIME_OFFSET_KWH", 100.0)
        after = await main.get_stats()
        assert after["total_kwh"] == pytest.approx(before["total_kwh"] + 100.0)
        assert after["recorded_kwh"] == before["recorded_kwh"]
        assert after["co2_saved_kg"] > before["co2_saved_kg"]
        assert after["money_saved"] > before["money_saved"]

    async def test_a_negative_offset_stops_at_zero(self, main, monkeypatch):
        await _reading(main.db, 1, 100.0)
        monkeypatch.setattr(main, "LIFETIME_OFFSET_KWH", -5000.0)
        stats = await main.get_stats()
        assert stats["total_kwh"] == 0.0
        assert stats["co2_saved_kg"] >= 0.0
        assert stats["money_saved"] >= 0.0
