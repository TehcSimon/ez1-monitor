"""Tests for the CSV export (GET /api/export): 5-minute slots from the raw
readings, daily totals from daily_aggregates, and the number format that
follows the dashboard's language.

Requests go through httpx's ASGITransport against app.main wired to a temp
database, like the `main` fixture in test_lifetime.py. The transport sends no
lifespan events, so neither the poller nor the background tasks start.
"""
import csv
import importlib
import io
import os
import tempfile
from datetime import date, datetime

import httpx
import pytest
import pytest_asyncio

from app.database import Database

SLOT_HEADER = ["time", "pv1_w", "pv2_w", "total_w", "inverter_temp_c", "grid_v", "grid_hz"]


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


@pytest_asyncio.fixture
async def client(db, db_path, monkeypatch):
    """app.main wired to the temp database. Importing it needs both variables."""
    monkeypatch.setenv("INVERTER_IP", "192.0.2.1")
    monkeypatch.setenv("DB_PATH", db_path)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "db", db)
    # The language comes from Accept-Language in these tests, not the env.
    monkeypatch.setattr(main, "DEFAULT_LANG", "")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _ts(hour: int, minute: int, second: int = 0, day: int = 1) -> int:
    """Epoch of a local time on a July day, as the poller stores it."""
    return int(datetime(2026, 7, day, hour, minute, second).timestamp())


async def _reading(db: Database, ts: int, p1, p2, online: bool = True, **extra) -> None:
    await db.insert_measurement(ts, p1=p1, p2=p2, e1=0.5, e2=0.5, te1=10, te2=10,
                                online=online, **extra)


async def _rows(client, resolution: str, lang: str) -> list[list[str]]:
    resp = await client.get(f"/api/export?resolution={resolution}",
                            headers={"Accept-Language": lang})
    assert resp.status_code == 200
    return list(csv.reader(io.StringIO(resp.text), delimiter=";" if lang.startswith("de") else ","))


class TestSlots:

    async def test_readings_in_one_slot_are_averaged(self, client, db):
        await _reading(db, _ts(12, 0), 100, 200, temp_c=40.0, grid_v=230.0, grid_hz=50.0)
        await _reading(db, _ts(12, 2, 30), 200, 300, temp_c=42.0, grid_v=232.0, grid_hz=50.02)
        await _reading(db, _ts(12, 5), 50, 50)
        rows = await _rows(client, "5min", "en")
        assert rows[0] == SLOT_HEADER
        assert rows[1] == ["2026-07-01 12:00", "150.0", "250.0", "400.0", "41.0", "231.0", "50.01"]
        # The next slot is its own row. Without temperature and grid values
        # (older firmware) those fields stay empty rather than read 0.
        assert rows[2] == ["2026-07-01 12:05", "50.0", "50.0", "100.0", "", "", ""]
        assert len(rows) == 3

    async def test_offline_markers_are_left_out(self, client, db):
        await _reading(db, _ts(12, 0), 100, 100)
        await _reading(db, _ts(12, 10), None, None, online=False)
        rows = await _rows(client, "5min", "en")
        assert [r[0] for r in rows[1:]] == ["2026-07-01 12:00"]

    async def test_german_uses_semicolons_and_a_decimal_comma(self, client, db):
        await _reading(db, _ts(12, 0), 100.4, 200, grid_hz=49.98)
        resp = await client.get("/api/export?resolution=5min",
                                headers={"Accept-Language": "de-DE,de;q=0.9"})
        lines = resp.text.splitlines()
        assert lines[0] == ";".join(SLOT_HEADER)
        assert lines[1] == "2026-07-01 12:00;100,4;200,0;300,4;;;49,98"

    async def test_an_empty_database_gives_the_header_only(self, client):
        assert await _rows(client, "5min", "en") == [SLOT_HEADER]


class TestDays:

    async def test_days_reach_beyond_the_raw_rows(self, client, db):
        await _reading(db, _ts(12, 0, day=1), 100, 100)
        await _reading(db, _ts(12, 0, day=2), 150, 150)
        await db.backfill_daily_aggregates()
        # Retention prunes the raw rows; the daily aggregates stay, and so
        # does the export.
        await db.delete_old_measurements(0)
        assert await _rows(client, "5min", "de") == [SLOT_HEADER]
        assert await _rows(client, "day", "de") == [
            ["date", "kwh", "peak_w"],
            ["2026-07-01", "1,000", "200"],
            ["2026-07-02", "1,000", "300"],
        ]


class TestDownload:

    async def test_is_a_csv_attachment_named_after_today(self, client):
        resp = await client.get("/api/export?resolution=day")
        assert resp.headers["content-type"].startswith("text/csv")
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert f'filename="ez1-monitor-day-{date.today().isoformat()}.csv"' in disposition

    async def test_5min_is_the_default(self, client):
        resp = await client.get("/api/export")
        assert resp.text.splitlines() == [",".join(SLOT_HEADER)]

    async def test_an_unknown_resolution_is_rejected(self, client):
        resp = await client.get("/api/export?resolution=week")
        assert resp.status_code == 422
