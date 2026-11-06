"""Tests for the poller's offline detection and its health signal.

The inverter is scripted and the database is a real temp SQLite file, like in
test_aggregates.py. Poller needs neither INVERTER_IP nor a reachable inverter.
"""
import asyncio
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.co2 import CarbonState
from app.database import Database
from app.poller import Poller

INTERVAL = 60

UP = SimpleNamespace(p1=100, p2=100, e1=1.0, e2=1.0, te1=5.0, te2=5.0)
DOWN = TimeoutError("no answer")


class FakeInverter:
    """Answers from a script: exceptions are raised, anything else returned.
    Past the end of the script it stays down."""

    def __init__(self, *script):
        self.script = list(script)
        self.polls = 0

    async def get_device_info(self):
        return None

    async def get_output_data(self):
        self.polls += 1
        answer = self.script.pop(0) if self.script else DOWN
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    """Stands in for datetime in app.poller, with every poll a minute later.
    Readings within the same second would replace each other."""

    def __init__(self):
        self.current = datetime(2024, 6, 1, 12)

    def now(self):
        self.current += timedelta(minutes=1)
        return self.current


def locked(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest_asyncio.fixture
async def poller(db_path, monkeypatch):
    monkeypatch.setattr("app.poller.datetime", Clock())
    database = Database(db_path)
    await database.init()
    p = Poller("127.0.0.1", 8050, INTERVAL, database, CarbonState(), price_per_kwh=0.30)
    p.inverter = FakeInverter()
    yield p
    await p.stop()


async def _polls(poller, *script):
    """Runs one poll per script entry and returns the waits they asked for."""
    poller.inverter = FakeInverter(*script)
    return [await poller._poll_once() for _ in script]


def _rows(db_path, online):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM measurements WHERE online = ?", (online,)
        ).fetchone()[0]
    finally:
        con.close()


async def _wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "not reached in time"
        await asyncio.sleep(0.005)


class TestOfflineDetection:
    """One dropped poll must not look like an outage. Regression guard: it used
    to write the offline marker right away, which showed a red error on the
    dashboard and paused polling for up to five minutes."""

    async def test_single_failed_poll_changes_nothing(self, poller, db_path):
        assert await _polls(poller, UP, DOWN) == [INTERVAL, INTERVAL]
        assert _rows(db_path, online=0) == 0

    async def test_consecutive_failures_mean_offline(self, poller, db_path):
        waits = await _polls(poller, UP, DOWN, DOWN, DOWN)
        assert waits == [INTERVAL, INTERVAL, INTERVAL, poller.standby_interval]
        assert _rows(db_path, online=0) == 1

    async def test_empty_answer_counts_as_failure(self, poller, db_path):
        waits = await _polls(poller, None, None, None)
        assert waits == [INTERVAL, INTERVAL, poller.standby_interval]
        assert _rows(db_path, online=0) == 1

    async def test_one_marker_per_offline_streak(self, poller, db_path):
        waits = await _polls(poller, *[DOWN] * 6)
        assert waits[2:] == [poller.standby_interval] * 4
        assert _rows(db_path, online=0) == 1

    async def test_success_resets_the_count(self, poller, db_path):
        await _polls(poller, DOWN, DOWN, UP, DOWN, DOWN)
        assert _rows(db_path, online=0) == 0

    async def test_recovery_returns_to_normal(self, poller, db_path):
        waits = await _polls(poller, DOWN, DOWN, DOWN, UP, DOWN, DOWN, DOWN)
        # Back at the normal interval right away, and the next streak is an
        # outage of its own.
        assert waits[3:] == [INTERVAL, INTERVAL, INTERVAL, poller.standby_interval]
        assert _rows(db_path, online=0) == 2
        assert _rows(db_path, online=1) == 1


class TestDatabaseFailures:
    """A database problem must not be mistaken for an offline inverter, and
    must not end the loop."""

    async def test_failed_insert_does_not_count_against_the_inverter(self, poller, monkeypatch):
        monkeypatch.setattr(poller.db, "insert_measurement", locked)
        poller.inverter = FakeInverter(UP)
        with pytest.raises(sqlite3.OperationalError):
            await poller._poll_once()
        assert poller._failed_polls == 0

    async def test_answer_ends_a_failure_streak_even_if_the_insert_fails(self, poller, monkeypatch):
        await _polls(poller, DOWN, DOWN)
        monkeypatch.setattr(poller.db, "insert_measurement", locked)
        poller.inverter = FakeInverter(UP)
        with pytest.raises(sqlite3.OperationalError):
            await poller._poll_once()
        assert poller._failed_polls == 0

    async def test_failed_marker_write_is_retried(self, poller, db_path, monkeypatch):
        await _polls(poller, DOWN, DOWN)
        with monkeypatch.context() as m:
            m.setattr(poller.db, "insert_measurement", locked)
            poller.inverter = FakeInverter(DOWN)
            with pytest.raises(sqlite3.OperationalError):
                await poller._poll_once()
        assert _rows(db_path, online=0) == 0
        await _polls(poller, DOWN)
        assert _rows(db_path, online=0) == 1

    async def test_failing_cycles_do_not_end_the_loop(self, poller, monkeypatch):
        monkeypatch.setattr(poller.db, "insert_measurement", locked)
        poller.interval = 0.01
        poller.inverter = FakeInverter(*[UP] * 1000)
        await poller.start()
        await _wait_until(lambda: poller.inverter.polls >= 3)
        assert not poller._task.done()

    async def test_failed_device_info_write_does_not_end_the_loop(self, poller, monkeypatch):
        # The first thing the loop does, outside of its cycle guard.
        async def device_info():
            return SimpleNamespace(deviceId="E1", devVer="1.0", minPower=30, maxPower=800)

        async def no_write(*args, **kwargs):
            pass
        monkeypatch.setattr(poller.db, "update_device_info", locked)
        monkeypatch.setattr(poller.db, "insert_measurement", no_write)
        poller.interval = 0.01
        poller.inverter = FakeInverter(*[UP] * 1000)
        poller.inverter.get_device_info = device_info
        await poller.start()
        await _wait_until(lambda: poller.inverter.polls >= 3)
        assert not poller._task.done()
        assert poller._device_info_ok is False


class TestHealth:
    """/health asks is_healthy: is the poller working, not is the inverter up."""

    def _limit(self, poller):
        return 3 * max(poller.interval, poller.standby_interval)

    async def test_not_healthy_before_start(self, poller):
        assert not poller.is_healthy()

    async def test_healthy_after_start(self, poller):
        await poller.start()
        assert poller.is_healthy()

    async def test_healthy_up_to_the_limit(self, poller):
        await poller.start()
        poller._last_cycle_at = time.monotonic() - self._limit(poller) + 5
        assert poller.is_healthy()
        poller._last_cycle_at = time.monotonic() - self._limit(poller) - 5
        assert not poller.is_healthy()

    async def test_limit_covers_the_longer_of_both_waits(self, poller):
        # Normal interval above the standby cap: the normal one is the longest.
        poller.interval = 600
        await poller.start()
        poller._last_cycle_at = time.monotonic() - 3 * 600 + 5
        assert poller.is_healthy()

    async def test_not_healthy_once_the_task_is_gone(self, poller):
        await poller.start()
        poller._task.cancel()
        await asyncio.gather(poller._task, return_exceptions=True)
        assert not poller.is_healthy()

    async def test_offline_inverter_keeps_the_heartbeat_going(self, poller, db_path):
        # All night long: every poll fails, and that's a completed cycle.
        poller.interval = 0.01
        await poller.start()
        await _wait_until(lambda: _rows(db_path, online=0) == 1)
        beat = poller._last_cycle_at
        await _wait_until(lambda: poller._last_cycle_at > beat)

    async def test_failing_cycles_stop_the_heartbeat(self, poller, monkeypatch):
        monkeypatch.setattr(poller.db, "insert_measurement", locked)
        poller.interval = 0.01
        poller.inverter = FakeInverter(*[UP] * 1000)
        await poller.start()
        beat = poller._last_cycle_at
        await _wait_until(lambda: poller.inverter.polls >= 3)
        assert poller._last_cycle_at == beat

    async def test_stuck_cycle_stops_the_heartbeat(self, poller):
        never = asyncio.Event()

        async def hang():
            await never.wait()
        poller.inverter.get_output_data = hang
        await poller.start()
        await asyncio.sleep(0.05)
        assert not poller._task.done()
        poller._last_cycle_at = time.monotonic() - self._limit(poller) - 5
        assert not poller.is_healthy()
