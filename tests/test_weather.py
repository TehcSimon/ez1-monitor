"""Tests for the optional weather forecast (app/weather.py). No test talks to
the network; all coordinates are made up."""
import asyncio
import json
import logging

import httpx
import pytest

from app import weather
from app.site_map import SiteState
from app.weather import (
    WeatherState, codes_between, current_conditions, load_cache, temperatures_between, parse_forecast, poll_loop, poll_once,
    radiation_by_hour, save_cache,
)
from app.yield_curve import WINDOW_DAYS, radiation_bins

HOUR = 3600
NOW = 1_000_000 * HOUR + 1800   # half past some hour


def answer(hours=(NOW - 1800, NOW + 1800, NOW + 5400)):
    return {"hourly": {
        "time": list(hours),
        "shortwave_radiation": [100.0, 200.0, None],
        "weather_code": [3, 61, None],
        "temperature_2m": [11.4, 12.56, None],
    }}


def filled(fetched_at=NOW):
    columns = parse_forecast(answer())
    return WeatherState(
        enabled=True, lat=10.0, lon=20.0, fetched_at=fetched_at, times=columns["times"],
        radiation=columns["radiation"], codes=columns["codes"], temperatures=columns["temperatures"],
    )


class TestParseForecast:
    def test_a_good_answer_gives_its_columns(self):
        assert parse_forecast(answer())["radiation"] == [100.0, 200.0, None]

    @pytest.mark.parametrize("payload", [
        None, [], {}, {"hourly": None}, {"hourly": {"time": []}},
        {"hourly": {"time": [], "shortwave_radiation": [], "weather_code": [], "temperature_2m": []}},
        {"hourly": {"time": [1, 2], "shortwave_radiation": [1], "weather_code": [1, 2], "temperature_2m": [1, 2]}},
    ])
    def test_anything_else_is_refused(self, payload):
        assert parse_forecast(payload) is None


class TestReading:
    def test_radiation_leaves_out_gaps(self):
        assert radiation_by_hour(filled(), NOW) == {NOW - 1800: 100.0, NOW + 1800: 200.0}

    def test_the_hour_in_progress_is_the_one_ending_next(self):
        assert current_conditions(filled(), NOW) == {"code": 61, "temp_c": 12.6}

    def test_exactly_on_the_hour_it_is_the_one_just_over(self):
        assert current_conditions(filled(), NOW - 1800)["code"] == 3

    def test_a_gap_in_the_data_means_no_conditions(self):
        assert current_conditions(filled(), NOW + 3600) is None

    def test_a_time_outside_the_data_means_no_conditions(self):
        assert current_conditions(filled(), NOW + 100 * HOUR, ) is None

    def test_old_data_is_not_used(self):
        state = filled(fetched_at=NOW - weather.STALE_AFTER_S - 1)
        assert radiation_by_hour(state, NOW) is None
        assert current_conditions(state, NOW) is None

    def test_without_any_data_nothing_is_there(self):
        assert radiation_by_hour(WeatherState(), NOW) is None
        assert current_conditions(WeatherState(), NOW) is None
        assert codes_between(WeatherState(), NOW - HOUR, NOW + HOUR, NOW) is None

    def test_codes_cover_every_hour_overlapping_the_span_but_gaps(self):
        assert codes_between(filled(), NOW - HOUR, NOW + HOUR, NOW) == [[NOW - 1800, 3], [NOW + 1800, 61]]

    def test_an_hour_ending_at_the_start_is_not_part_of_it(self):
        assert codes_between(filled(), NOW - 1800, NOW, NOW) == [[NOW + 1800, 61]]

    def test_a_span_without_codes_has_none(self):
        assert codes_between(filled(), NOW + 10 * HOUR, NOW + 20 * HOUR, NOW) is None

    def test_temperatures_reach_one_hour_beyond_both_ends(self):
        assert temperatures_between(filled(), NOW, NOW, NOW) == [[NOW - 1800, 11.4], [NOW + 1800, 12.6]]

    def test_temperatures_leave_out_gaps_and_old_data(self):
        assert temperatures_between(filled(), NOW + 2 * HOUR, NOW + 3 * HOUR, NOW) is None
        state = filled(fetched_at=NOW - weather.STALE_AFTER_S - 1)
        assert temperatures_between(state, NOW, NOW, NOW) is None

    def test_old_codes_are_not_used(self):
        state = filled(fetched_at=NOW - weather.STALE_AFTER_S - 1)
        assert codes_between(state, NOW - HOUR, NOW + HOUR, NOW) is None


class TestWhatIsRequested:
    @pytest.mark.parametrize("offset", [-12 * HOUR, -8 * HOUR, 0, HOUR, 12 * HOUR, 14 * HOUR])
    @pytest.mark.parametrize("hour", range(24))
    def test_it_covers_every_day_the_card_needs_at_any_hour(self, offset, hour):
        now = 20_000 * 86400 + hour * HOUR + 1800
        first = (now // 86400 - weather.PAST_DAYS) * 86400
        hours = (weather.PAST_DAYS + weather.FORECAST_DAYS) * 24
        hourly = {first + h * HOUR: 100.0 for h in range(hours)}
        today = (now + offset) // 86400
        for day in range(today - WINDOW_DAYS, today + 2):
            assert radiation_bins(hourly, day * 86400 - offset) is not None


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestPollOnce:
    @pytest.fixture
    def state(self, tmp_path):
        return WeatherState(enabled=True, lat=10.0, lon=20.0, cache_path=tmp_path / weather.WEATHER_CACHE_FILE)

    async def test_a_good_answer_is_taken_and_kept(self, state):
        async with client_for(lambda request: httpx.Response(200, json=answer())) as client:
            assert await poll_once(state, client)
        assert state.radiation[1] == 200.0
        assert state.fetched_at is not None
        assert state.cache_path.exists()

    async def test_it_asks_for_the_history_and_identifies_the_app(self, state):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=answer())
        async with client_for(handler) as client:
            await poll_once(state, client)
        query = seen[0].url.params
        assert query["latitude"] == "10.0" and query["longitude"] == "20.0"
        assert int(query["past_days"]) == weather.WINDOW_DAYS + 1
        assert seen[0].headers["user-agent"].startswith("ez1-monitor/")

    @pytest.mark.parametrize("response", [
        httpx.Response(429), httpx.Response(200, text="not json"), httpx.Response(200, json={"error": True}),
    ])
    async def test_a_bad_answer_changes_nothing(self, state, response):
        async with client_for(lambda request: response) as client:
            assert not await poll_once(state, client)
        assert state.fetched_at is None and not state.cache_path.exists()

    async def test_a_connection_error_is_only_a_failed_poll(self, state):
        def handler(request):
            raise httpx.ConnectError("no route")
        async with client_for(handler) as client:
            assert not await poll_once(state, client)

    async def test_the_coordinates_are_never_logged(self, state, caplog):
        caplog.set_level(logging.DEBUG, logger="app.weather")
        state.lat, state.lon = 12.34, 56.78
        async with client_for(lambda request: httpx.Response(500)) as client:
            await poll_once(state, client)
        assert "HTTP 500" in caplog.text
        assert "12.34" not in caplog.text and "56.78" not in caplog.text


class TestCache:
    def test_a_restart_finds_the_last_answer(self, tmp_path):
        state = filled()
        state.cache_path = tmp_path / "w.json"
        save_cache(state)
        fresh = WeatherState(enabled=True, lat=10.0, lon=20.0, cache_path=state.cache_path)
        load_cache(fresh)
        assert fresh.fetched_at == NOW and fresh.radiation == state.radiation

    def test_another_place_starts_over(self, tmp_path):
        state = filled()
        state.cache_path = tmp_path / "w.json"
        save_cache(state)
        fresh = WeatherState(enabled=True, lat=11.0, lon=20.0, cache_path=state.cache_path)
        load_cache(fresh)
        assert fresh.fetched_at is None

    @pytest.mark.parametrize("content", ["", "{broken", "[]", json.dumps({"version": 1, "lat": 10.0, "lon": 20.0})])
    def test_a_damaged_cache_is_ignored(self, tmp_path, content):
        path = tmp_path / "w.json"
        path.write_text(content, encoding="utf-8")
        state = WeatherState(enabled=True, lat=10.0, lon=20.0, cache_path=path)
        load_cache(state)
        assert state.fetched_at is None

    def test_an_unwritable_cache_is_not_fatal(self, tmp_path):
        state = filled()
        state.cache_path = tmp_path / "missing" / "w.json"
        save_cache(state)


class TestPollLoop:
    async def test_off_by_default(self):
        await asyncio.wait_for(poll_loop(WeatherState(), SiteState(address="x")), 1)

    async def test_without_a_location_it_stays_off(self, caplog):
        caplog.set_level(logging.WARNING, logger="app.weather")
        await asyncio.wait_for(poll_loop(WeatherState(enabled=True), SiteState()), 1)
        assert "SITE_ADDRESS" in caplog.text

    async def test_only_rounded_coordinates_are_used(self, monkeypatch):
        seen = []
        async def fake_poll(state, client):
            seen.append((state.lat, state.lon))
            raise asyncio.CancelledError
        monkeypatch.setattr(weather, "poll_once", fake_poll)
        site = SiteState(address="x", lat=51.31373, lon=9.49766)
        await asyncio.wait_for(poll_loop(WeatherState(enabled=True), site), 1)
        assert seen == [(51.31, 9.5)]

    async def test_a_recent_answer_is_not_fetched_again(self, tmp_path, monkeypatch):
        import time
        state = filled(fetched_at=time.time())
        state.cache_path = tmp_path / "w.json"
        save_cache(state)
        polled, slept = [], []
        async def fake_poll(state, client):
            polled.append(1)
            return True
        async def fake_sleep(seconds):
            slept.append(seconds)
            raise asyncio.CancelledError
        monkeypatch.setattr(weather, "poll_once", fake_poll)
        monkeypatch.setattr(weather.asyncio, "sleep", fake_sleep)
        fresh = WeatherState(enabled=True, cache_path=state.cache_path)
        await poll_loop(fresh, SiteState(address="x", lat=10.0, lon=20.0))
        assert not polled
        assert 3000 < slept[0] <= weather.POLL_INTERVAL_S
