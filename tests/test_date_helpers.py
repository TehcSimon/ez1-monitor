"""Tests for the date-math helpers used by the stat aggregations.

These helpers are pure functions, so we can test them exhaustively without
touching the database or HTTP layer. The edge cases that motivated this
suite are leap-year handling for shift_year() and month-length variations
for last_day_of_month().
"""
from datetime import datetime

import pytest

from app.date_helpers import last_day_of_month, shift_year


class TestShiftYear:
    def test_default_shifts_one_year_back(self):
        result = shift_year(datetime(2025, 6, 15, 14, 30))
        assert result == datetime(2024, 6, 15, 14, 30)

    def test_explicit_negative_shift(self):
        result = shift_year(datetime(2025, 6, 15), -2)
        assert result == datetime(2023, 6, 15)

    def test_positive_shift(self):
        result = shift_year(datetime(2024, 6, 15), 1)
        assert result == datetime(2025, 6, 15)

    def test_zero_shift_is_identity(self):
        original = datetime(2024, 6, 15, 14, 30, 45)
        assert shift_year(original, 0) == original

    def test_preserves_time_of_day(self):
        result = shift_year(datetime(2025, 3, 10, 23, 59, 59, 999999))
        assert result == datetime(2024, 3, 10, 23, 59, 59, 999999)

    def test_leap_day_to_non_leap_year_clamps_to_feb_28(self):
        # Feb 29, 2024 (leap) shifted back one year — 2023 has no Feb 29
        result = shift_year(datetime(2024, 2, 29))
        assert result == datetime(2023, 2, 28)

    def test_leap_day_to_leap_year_preserves_feb_29(self):
        # Feb 29, 2024 shifted back 4 years — 2020 is also a leap year
        result = shift_year(datetime(2024, 2, 29), -4)
        assert result == datetime(2020, 2, 29)

    def test_leap_day_forward_to_non_leap_year_clamps(self):
        result = shift_year(datetime(2024, 2, 29), 1)
        assert result == datetime(2025, 2, 28)

    def test_century_year_not_leap(self):
        # 1900 was NOT a leap year despite being divisible by 4
        # (Gregorian rule: century years must be divisible by 400)
        result = shift_year(datetime(2000, 2, 29), -100)
        assert result == datetime(1900, 2, 28)

    def test_year_boundary_dec_31(self):
        result = shift_year(datetime(2025, 12, 31, 23, 59))
        assert result == datetime(2024, 12, 31, 23, 59)

    def test_year_boundary_jan_1(self):
        result = shift_year(datetime(2025, 1, 1, 0, 0))
        assert result == datetime(2024, 1, 1, 0, 0)


class TestLastDayOfMonth:
    @pytest.mark.parametrize("month,expected_day", [
        (1, 31),   # January
        (2, 28),   # February non-leap
        (3, 31),
        (4, 30),
        (5, 31),
        (6, 30),
        (7, 31),
        (8, 31),
        (9, 30),
        (10, 31),
        (11, 30),
        (12, 31),
    ])
    def test_non_leap_year_2025(self, month, expected_day):
        result = last_day_of_month(datetime(2025, month, 1))
        assert result.day == expected_day
        assert result.month == month
        assert result.year == 2025

    def test_leap_year_february(self):
        result = last_day_of_month(datetime(2024, 2, 1))
        assert result == datetime(2024, 2, 29, 23, 59, 59, 999999)

    def test_non_leap_year_february(self):
        result = last_day_of_month(datetime(2025, 2, 1))
        assert result == datetime(2025, 2, 28, 23, 59, 59, 999999)

    def test_century_non_leap_february(self):
        # 1900 was not a leap year
        result = last_day_of_month(datetime(1900, 2, 15))
        assert result.day == 28

    def test_400_year_leap_february(self):
        # 2000 was a leap year (divisible by 400)
        result = last_day_of_month(datetime(2000, 2, 15))
        assert result.day == 29

    def test_december_rolls_over_year(self):
        result = last_day_of_month(datetime(2025, 12, 5))
        assert result == datetime(2025, 12, 31, 23, 59, 59, 999999)

    def test_returns_last_microsecond(self):
        # The result is meant to be used as an inclusive upper bound:
        # any timestamp strictly less than this is inside the month.
        result = last_day_of_month(datetime(2025, 6, 15))
        assert result == datetime(2025, 6, 30, 23, 59, 59, 999999)

    def test_input_day_of_month_irrelevant(self):
        # The input's day-of-month doesn't matter — only its month does
        for day in (1, 15, 28, 30):
            result = last_day_of_month(datetime(2025, 4, day))
            assert result.day == 30
            assert result.month == 4

    def test_input_time_of_day_irrelevant(self):
        result_morning = last_day_of_month(datetime(2025, 6, 1, 0, 0, 0))
        result_evening = last_day_of_month(datetime(2025, 6, 1, 23, 59, 59))
        assert result_morning == result_evening


class TestCombinedUsage:
    """Tests for the way these helpers are combined in get_stats()."""

    def test_year_over_year_month_end_clamps_correctly(self):
        # On Mar 31, "same period last year" is also Mar 31 (no clamping)
        ref = shift_year(datetime(2025, 3, 31, 18, 0))
        assert ref == datetime(2024, 3, 31, 18, 0)

    def test_year_over_year_leap_day_clamps_to_feb_28(self):
        # On Feb 29, 2024, "same period last year" lands on Feb 28, 2023
        # because 2023 has no Feb 29
        ref = shift_year(datetime(2024, 2, 29, 12, 0))
        assert ref == datetime(2023, 2, 28, 12, 0)

    def test_last_month_was_february_in_a_31_day_year(self):
        # On Mar 31, "same progress in previous month" would be Feb 31
        # which doesn't exist. The caller clamps this to last_day_of_month.
        # Verify the clamp target.
        feb_start = datetime(2025, 2, 1)
        assert last_day_of_month(feb_start) == datetime(
            2025, 2, 28, 23, 59, 59, 999999
        )
