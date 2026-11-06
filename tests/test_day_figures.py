"""Tests for the per-panel figures above the day curve.

summarize_day lives in app.day_figures (a pure module, no FastAPI/inverter
imports) and works on rows shaped like those of Database.get_range.
"""
import pytest

from app.day_figures import summarize_day

NOON = 1_750_000_000
MIN = 60


def row(minute: int, p1, p2, e1=0.0, e2=0.0, online=True) -> dict:
    return {
        "timestamp": NOON + minute * MIN, "p1": p1, "p2": p2,
        "e1": e1, "e2": e2, "te1": 10.0, "te2": 10.0, "online": online,
    }


class TestNoData:

    def test_no_rows(self):
        for figures in summarize_day([]).values():
            assert figures == {"peak_w": 0.0, "peak_ts": None, "avg_w": None, "kwh": 0.0}

    def test_offline_marker_only(self):
        marker = row(0, None, None, None, None, online=False)
        assert summarize_day([marker])["total"]["peak_ts"] is None

    def test_online_but_dark(self):
        # Rows exist, nothing produced yet: still no peak and no average.
        figures = summarize_day([row(0, 0, 0), row(1, 0, 0)])
        assert figures["total"] == {"peak_w": 0.0, "peak_ts": None, "avg_w": None, "kwh": 0.0}


class TestPeak:

    def test_each_row_has_its_own_peak_and_time(self):
        figures = summarize_day([row(0, 300, 100), row(1, 200, 290), row(2, 280, 280)])
        assert figures["pv1"]["peak_w"] == 300.0
        assert figures["pv1"]["peak_ts"] == NOON
        assert figures["pv2"]["peak_w"] == 290.0
        assert figures["pv2"]["peak_ts"] == NOON + MIN
        # The panels peaked at different times: the array's peak is lower than
        # the sum of both, and at a time of its own.
        assert figures["total"]["peak_w"] == 560.0
        assert figures["total"]["peak_ts"] == NOON + 2 * MIN

    def test_earliest_time_when_reached_twice(self):
        figures = summarize_day([row(0, 100, 0), row(1, 300, 0), row(2, 300, 0)])
        assert figures["pv1"]["peak_ts"] == NOON + MIN

    def test_offline_rows_do_not_count(self):
        figures = summarize_day([row(0, 100, 100), row(1, 900, 900, online=False)])
        assert figures["total"]["peak_w"] == 200.0

    def test_missing_power_counts_as_zero(self):
        figures = summarize_day([row(0, None, 200)])
        assert figures["pv1"]["peak_w"] == 0.0
        assert figures["pv1"]["peak_ts"] is None
        assert figures["pv2"]["peak_w"] == 200.0


class TestEnergy:

    def test_largest_counter_value_per_panel(self):
        figures = summarize_day([row(0, 1, 1, 1.0, 0.5), row(1, 1, 1, 2.5, 1.5)])
        assert figures["pv1"]["kwh"] == 2.5
        assert figures["pv2"]["kwh"] == 1.5

    def test_panels_add_up_to_the_total(self):
        figures = summarize_day([row(0, 1, 1, 2.123, 1.456)])
        assert figures["total"]["kwh"] == pytest.approx(
            figures["pv1"]["kwh"] + figures["pv2"]["kwh"]
        )

    def test_missing_counter_counts_as_zero(self):
        assert summarize_day([row(0, 1, 1, None, 1.0)])["pv1"]["kwh"] == 0.0


class TestAverage:

    def test_energy_over_the_production_window(self):
        # 60 minutes of production, 0.3 + 0.1 kWh.
        figures = summarize_day([row(0, 300, 100), row(60, 300, 100, 0.3, 0.1)])
        assert figures["pv1"]["avg_w"] == 300.0
        assert figures["pv2"]["avg_w"] == 100.0
        assert figures["total"]["avg_w"] == 400.0

    def test_window_is_the_same_for_a_shaded_panel(self):
        # PV 2 only produces in the last 15 of 60 minutes. It keeps the
        # array's window, or its average would hide the shading.
        points = [row(0, 200, 0), row(45, 200, 0), row(46, 200, 200), row(60, 200, 200, 0.2, 0.05)]
        figures = summarize_day(points)
        assert figures["pv2"]["avg_w"] == 50.0

    def test_panels_add_up_to_the_total(self):
        figures = summarize_day([row(0, 200, 200), row(100, 200, 200, 0.33, 0.17)])
        assert figures["pv1"]["avg_w"] + figures["pv2"]["avg_w"] == pytest.approx(
            figures["total"]["avg_w"], abs=0.1
        )

    def test_threshold_applies_to_the_whole_array(self):
        # The rows below the threshold at both ends don't stretch the window.
        # 3 + 3 W do count: below it per panel, above it together.
        points = [row(0, 2, 1), row(30, 3, 3), row(90, 300, 300, 0.3, 0.3), row(120, 1, 1)]
        figures = summarize_day(points)
        assert figures["total"]["avg_w"] == 600.0

    def test_dead_panel_shows_zero_not_nothing(self):
        figures = summarize_day([row(0, 300, 0), row(60, 300, 0, 0.3, 0.0)])
        assert figures["pv2"] == {"peak_w": 0.0, "peak_ts": None, "avg_w": 0.0, "kwh": 0.0}

    def test_single_reading_has_no_window(self):
        figures = summarize_day([row(0, 300, 100, 0.1, 0.1)])
        assert figures["total"]["avg_w"] is None
        assert figures["total"]["peak_w"] == 400.0
