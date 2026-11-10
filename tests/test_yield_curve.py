"""Tests for the learned target and the projection (app/yield_curve.py), and
the endpoint built on them, against a real temp SQLite file."""
import importlib
import os
import tempfile
import time as time_module
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio

from app.database import Database
from app.weather import WeatherState
from app.yield_curve import (
    BIN_SECONDS, BINS, MAX_HISTORY_WEIGHT, MIN_LEARNED_DAYS, WINDOW_DAYS, bins_of_day, curve_kwh,
    day_start_ts, forecast_curve, learned_response, learned_target, project_today, radiation_bins,
    standard_offset_seconds,
)


def flat(watts):
    return [float(watts)] * BINS


class TestLearnedTarget:
    def test_too_few_days_learn_nothing(self):
        assert learned_target([flat(100)] * (MIN_LEARNED_DAYS - 1)) is None

    def test_enough_days_learn_a_curve(self):
        target = learned_target([flat(100)] * MIN_LEARNED_DAYS)
        assert target == pytest.approx(flat(100))

    def test_the_curve_follows_the_good_days_not_the_best_one(self):
        days = [flat(w) for w in (*[10] * 18, 300, 400, 900)]
        # Index int(0.9 * 21) = 18 of the sorted days: neither the 900 nor the 10s.
        assert learned_target(days)[100] == pytest.approx(300)

    def test_neighbouring_slots_are_smoothed(self):
        day = flat(0)
        day[100] = 500.0
        target = learned_target([day] * MIN_LEARNED_DAYS)
        assert target[100] == pytest.approx(100)
        assert target[98] == pytest.approx(100)
        assert target[97] == 0


class TestBinsOfDay:
    START = 1_000_000

    def row(self, offset, p1=100, p2=50, online=1):
        return {"timestamp": self.START + offset, "p1": p1, "p2": p2, "online": online}

    def test_a_slot_is_the_average_of_its_readings(self):
        p1, p2 = bins_of_day([self.row(0, 100, 10), self.row(60, 200, 30)], self.START)
        assert p1[0] == 150 and p2[0] == 20

    def test_offline_readings_do_not_count(self):
        p1, _ = bins_of_day([self.row(0, 100), self.row(60, 300, online=0)], self.START)
        assert p1[0] == 100

    def test_missing_values_count_as_nothing(self):
        p1, _ = bins_of_day([self.row(0, None)], self.START)
        assert p1[0] == 0

    @pytest.mark.parametrize("offset", [-60, BINS * BIN_SECONDS])
    def test_readings_outside_the_frame_are_skipped(self, offset):
        p1, _ = bins_of_day([self.row(offset)], self.START)
        assert sum(p1) == 0


class TestProjection:
    TARGET = [0.0] * 100 + [1200.0] * 100 + [0.0] * 88   # 10 kWh over the day

    def actual(self, watts):
        return [0.0] * 100 + [float(watts)] * 100 + [0.0] * 88

    def test_nothing_without_a_target(self):
        assert project_today(flat(1), None, 150, 1.0) == {"kwh": None, "done": False}

    def test_nothing_early_in_the_day(self):
        assert project_today(self.actual(600), self.TARGET, 105, 0.5)["kwh"] is None

    def test_a_day_on_target_ends_on_target(self):
        result = project_today(self.actual(1200), self.TARGET, 150, 5.0)
        assert result == {"kwh": pytest.approx(10.0), "done": False}

    def test_half_the_target_so_far_halves_the_rest(self):
        assert project_today(self.actual(600), self.TARGET, 150, 2.5)["kwh"] == pytest.approx(5.0)

    def test_a_day_above_target_ends_above_it(self):
        assert project_today(self.actual(1500), self.TARGET, 150, 6.25)["kwh"] == pytest.approx(12.5)

    def test_the_counter_is_the_base_not_the_power_curve(self):
        # The inverter's counter runs 4 % ahead of the integrated power here.
        assert project_today(self.actual(1200), self.TARGET, 150, 5.2)["kwh"] == pytest.approx(10.2)

    def test_at_the_end_of_the_day_it_is_the_counter(self):
        result = project_today(self.actual(600), self.TARGET, 200, 5.1)
        assert result == {"kwh": pytest.approx(5.1), "done": True}

    def test_an_empty_target_says_nothing(self):
        assert project_today(flat(0), flat(0), 150, 0.0)["kwh"] is None


class TestProjectionWithForecast:
    TARGET = TestProjection.TARGET
    FORECAST = [0.0] * 100 + [600.0] * 100 + [0.0] * 88   # half a clear day

    def actual(self, watts):
        return [0.0] * 100 + [float(watts)] * 100 + [0.0] * 88

    def test_before_the_first_watt_it_is_the_forecast(self):
        result = project_today(flat(0), self.TARGET, 0, 0.0, self.FORECAST)
        assert result == {"kwh": pytest.approx(5.0), "done": False}

    def test_the_share_rule_does_not_hold_it_back_early_on(self):
        assert project_today(self.actual(600), self.TARGET, 105, 0.25, self.FORECAST)["kwh"] is not None

    def test_early_in_the_day_the_forecast_counts_for_most(self):
        # 10 % through: a bright start shouldn't outweigh a dull forecast.
        result = project_today(self.actual(1200), self.TARGET, 110, 1.0, self.FORECAST)
        rest_of_forecast, rest_of_history = 4.5, 9.0
        assert result["kwh"] == pytest.approx(1.0 + 0.9 * rest_of_forecast + 0.1 * rest_of_history)

    def test_the_history_never_counts_for_more_than_half(self):
        result = project_today(self.actual(1200), self.TARGET, 180, 8.0, self.FORECAST)
        rest_of_forecast, rest_of_history = 1.0, 2.0
        assert MAX_HISTORY_WEIGHT == 0.5
        assert result["kwh"] == pytest.approx(8.0 + 0.5 * rest_of_forecast + 0.5 * rest_of_history)

    def test_after_the_last_slot_only_the_counter_is_left(self):
        result = project_today(self.actual(600), self.TARGET, BINS, 5.1, self.FORECAST)
        assert result == {"kwh": pytest.approx(5.1), "done": True}

    def test_without_a_target_a_forecast_alone_says_nothing(self):
        assert project_today(flat(1), None, 150, 1.0, self.FORECAST)["kwh"] is None


class TestRadiationBins:
    START = 3600 * 1000

    def hourly(self, value=lambda h: 100.0, hours=26):
        return {self.START + 3600 * h: value(h) for h in range(hours)}

    def test_constant_radiation_stays_constant(self):
        assert radiation_bins(self.hourly(), self.START) == pytest.approx([100.0] * BINS)

    def test_the_value_at_an_hour_end_belongs_to_the_hour_before(self):
        # Only the hour ending at 13:00 is bright: its peak is at 12:30.
        slots = radiation_bins(self.hourly(lambda h: 500.0 if h == 13 else 0.0), self.START)
        assert slots.index(max(slots)) in (149, 150)
        assert slots[12 * 12 - 12] == 0.0

    def test_between_two_hours_it_is_interpolated(self):
        slots = radiation_bins(self.hourly(lambda h: 0.0 if h < 13 else 120.0), self.START)
        assert 0 < slots[12 * 12] < 120

    def test_a_day_the_data_does_not_cover_gives_none(self):
        assert radiation_bins(self.hourly(hours=12), self.START) is None

    def test_a_gap_gives_none(self):
        hourly = self.hourly()
        del hourly[self.START + 3600 * 5]
        assert radiation_bins(hourly, self.START) is None


class TestLearnedResponse:
    def test_too_few_days_learn_nothing(self):
        assert learned_response([flat(100)] * 6, [flat(500)] * 6) is None

    def test_it_is_watts_per_radiation(self):
        assert learned_response([flat(100)] * 7, [flat(500)] * 7) == pytest.approx([0.2] * BINS)

    def test_a_cloudy_day_counts_with_what_it_delivered(self):
        response = learned_response([flat(300)] * 6 + [flat(30)], [flat(500)] * 6 + [flat(50)])
        assert response[100] == pytest.approx(1830 / 3050)

    def test_a_shaded_time_of_day_learns_a_low_value(self):
        power = [100.0] * BINS
        for b in range(150, 200):
            power[b] = 10.0
        response = learned_response([power] * 7, [flat(500)] * 7)
        assert response[175] == pytest.approx(0.02)
        assert response[100] == pytest.approx(0.2)

    def test_the_night_learns_nothing(self):
        assert learned_response([flat(5)] * 7, [flat(0)] * 7) == [0.0] * BINS

    def test_the_curve_is_response_times_radiation(self):
        assert forecast_curve([0.5, 0.2], [100.0, 200.0]) == [50.0, 40.0]

    def test_the_total_of_a_curve(self):
        assert curve_kwh(flat(1000)) == pytest.approx(24.0)


class TestDayFrame:
    @pytest.fixture
    def berlin(self, monkeypatch):
        # A POSIX rule, so the test needs no tzdata in the image.
        monkeypatch.setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3")
        time_module.tzset()
        yield
        monkeypatch.undo()
        time_module.tzset()

    def test_the_offset_ignores_daylight_saving(self, berlin):
        assert standard_offset_seconds(2026) == 3600

    def test_a_slot_means_the_same_sun_all_year(self, berlin):
        offset = standard_offset_seconds(2026)
        winter = datetime(2026, 1, 15, 12, 0).timestamp()
        summer = datetime(2026, 7, 15, 13, 0).timestamp()   # 12:00 standard time
        assert (winter - day_start_ts(date(2026, 1, 15), offset)) == (summer - day_start_ts(date(2026, 7, 15), offset))


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.unlink(path + suffix)


@pytest_asyncio.fixture
async def main(db_path, monkeypatch):
    monkeypatch.setenv("INVERTER_IP", "192.0.2.1")
    monkeypatch.setenv("DB_PATH", db_path)
    module = importlib.import_module("app.main")
    database = Database(db_path)
    await database.init()
    monkeypatch.setattr(module, "db", database)
    return module


async def fill_day(db, days_ago, watts, kwh=1.0):
    """Readings from 09:00 to 11:00 standard time, split over both panels."""
    day = date.today() - timedelta(days=days_ago)
    start = day_start_ts(day, standard_offset_seconds(day.year)) + 9 * 3600
    for minute in range(0, 120, 5):
        await db.insert_measurement(start + minute * 60, watts / 2, watts / 2, kwh / 2, kwh / 2, 10.0, 10.0, online=True)


class TestYieldCurveEndpoint:
    async def test_a_new_install_has_no_target_yet(self, main):
        await fill_day(main.db, 1, 400)
        answer = await main.get_yield_curve(date=(date.today() - timedelta(days=1)).isoformat())
        assert answer["target"] is None
        assert answer["learned_days"] == 0
        assert answer["needed_days"] == MIN_LEARNED_DAYS
        assert answer["kwh"] == pytest.approx(1.0)
        assert len(answer["p1"]) == len(answer["p2"]) == BINS

    async def test_the_target_comes_from_earlier_days_only(self, main):
        for days_ago in range(2, 2 + MIN_LEARNED_DAYS):
            await fill_day(main.db, days_ago, 400)
        await fill_day(main.db, 1, 800)
        answer = await main.get_yield_curve(date=(date.today() - timedelta(days=1)).isoformat())
        assert answer["learned_days"] == MIN_LEARNED_DAYS
        assert max(answer["target"]) == pytest.approx(400)
        assert max(a + b for a, b in zip(answer["p1"], answer["p2"])) == pytest.approx(800)

    async def test_days_beyond_the_window_are_forgotten(self, main):
        for days_ago in range(WINDOW_DAYS + 2, WINDOW_DAYS + 2 + MIN_LEARNED_DAYS):
            await fill_day(main.db, days_ago, 400)
        answer = await main.get_yield_curve(date=(date.today() - timedelta(days=1)).isoformat())
        assert answer["learned_days"] == 0

    async def test_only_today_gets_a_projection(self, main):
        for days_ago in range(1, 2 + MIN_LEARNED_DAYS):
            await fill_day(main.db, days_ago, 400)
        past = await main.get_yield_curve(date=(date.today() - timedelta(days=1)).isoformat())
        assert past["is_today"] is False
        assert past["projection_kwh"] is None
        today = await main.get_yield_curve(date=None)
        assert today["is_today"] is True
        assert today["date"] == date.today().isoformat()

    async def test_a_date_in_the_future_means_today(self, main):
        answer = await main.get_yield_curve(date=(date.today() + timedelta(days=3)).isoformat())
        assert answer["date"] == date.today().isoformat()


def sunny_forecast(radiation=500.0, code=1, temp=20.0):
    """A forecast covering the whole learning window, today and tomorrow."""
    now = int(datetime.now().timestamp())
    first = now - now % 3600 - 24 * 3600 * 24
    times = [first + 3600 * h for h in range(24 * 28)]
    return WeatherState(
        enabled=True, lat=10.0, lon=20.0, fetched_at=now, times=times, radiation=[radiation] * len(times),
        codes=[code] * len(times), temperatures=[temp] * len(times),
    )


class TestForecastInTheEndpoint:
    async def test_without_a_forecast_nothing_changes(self, main):
        for days_ago in range(1, 2 + MIN_LEARNED_DAYS):
            await fill_day(main.db, days_ago, 400)
        answer = await main.get_yield_curve(date=None)
        assert answer["forecast"] is None
        assert answer["tomorrow_kwh"] is None
        assert answer["weather"] is None

    async def test_the_forecast_reaches_the_page(self, main, monkeypatch):
        for days_ago in range(1, 2 + MIN_LEARNED_DAYS):
            await fill_day(main.db, days_ago, 400)
        monkeypatch.setattr(main, "weather_state", sunny_forecast())
        answer = await main.get_yield_curve(date=None)
        # Two hours at 400 W, the same sky as on the days it learned from.
        assert sum(answer["forecast"]) / 12 / 1000 == pytest.approx(0.8, abs=0.01)
        assert answer["tomorrow_kwh"] == pytest.approx(0.8, abs=0.01)
        assert answer["projection_kwh"] is not None
        assert answer["weather"] == {"code": 1, "temp_c": 20.0}

    async def test_a_darker_forecast_lowers_the_outlook(self, main, monkeypatch):
        for days_ago in range(1, 2 + MIN_LEARNED_DAYS):
            await fill_day(main.db, days_ago, 400)
        state = sunny_forecast()
        tomorrow = int(datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).timestamp())
        state.radiation = [r if t < tomorrow - 7200 else r / 2 for t, r in zip(state.times, state.radiation)]
        monkeypatch.setattr(main, "weather_state", state)
        answer = await main.get_yield_curve(date=None)
        assert answer["tomorrow_kwh"] == pytest.approx(0.4, abs=0.02)

    async def test_a_past_day_gets_no_forecast(self, main, monkeypatch):
        for days_ago in range(2, 3 + MIN_LEARNED_DAYS):
            await fill_day(main.db, days_ago, 400)
        monkeypatch.setattr(main, "weather_state", sunny_forecast())
        answer = await main.get_yield_curve(date=(date.today() - timedelta(days=1)).isoformat())
        assert answer["forecast"] is None and answer["weather"] is None

    async def test_too_little_history_gives_no_forecast(self, main, monkeypatch):
        await fill_day(main.db, 1, 400)
        monkeypatch.setattr(main, "weather_state", sunny_forecast())
        answer = await main.get_yield_curve(date=None)
        assert answer["forecast"] is None and answer["tomorrow_kwh"] is None


class TestSiteEndpoint:
    async def test_off_by_default(self, main):
        assert await main.get_site() == {"configured": False, "ready": False}


class TestTemperaturesEndpoint:
    async def test_the_last_three_hours_of_the_inverter(self, main):
        now = int(datetime.now().timestamp())
        await main.db.insert_measurement(now - 4 * 3600, 0, 0, 0, 0, 0, 0, online=True, temp_c=10.0)
        await main.db.insert_measurement(now - 3600, 0, 0, 0, 0, 0, 0, online=True, temp_c=31.5)
        await main.db.insert_measurement(now - 60, 0, 0, 0, 0, 0, 0, online=True)
        answer = await main.get_temperatures()
        assert answer["inverter"] == [[now - 3600, 31.5]]
        assert answer["window_s"] == 3 * 3600

    async def test_outside_only_with_a_forecast(self, main, monkeypatch):
        assert (await main.get_temperatures())["outside"] is None
        monkeypatch.setattr(main, "weather_state", sunny_forecast(temp=18.0))
        outside = (await main.get_temperatures())["outside"]
        assert len(outside) >= 4 and {v for _, v in outside} == {18.0}
