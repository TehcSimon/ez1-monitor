"""Date math helpers for stat aggregations.

Pure functions, extracted into their own module so they can be unit-tested
without importing the full FastAPI app (which initializes the database,
poller, and HTTP client at import time).
"""
from datetime import datetime, timedelta, date as date_cls


def iso_week_monday(iso_year: int, iso_week: int) -> date_cls:
    """Monday (date) of the given ISO year/week.

    Mirrors the computation used in the Hall-of-Fame best-week query so the
    week boundaries are identical everywhere. Note: passing iso_week=53 for a
    year that only has 52 ISO weeks yields a Monday in the next ISO year — the
    caller's date-range lookup then simply finds no data, which is the desired
    "gate it off" behaviour for the year-over-year comparison.
    """
    jan4 = date_cls(iso_year, 1, 4)
    week1_monday = jan4 - timedelta(days=jan4.isoweekday() - 1)
    return week1_monday + timedelta(weeks=iso_week - 1)


def iso_week_of(d) -> tuple:
    """Return (iso_year, iso_week) for a date or datetime."""
    c = d.isocalendar()
    return (c[0], c[1])


def shift_year(dt: datetime, years: int = -1) -> datetime:
    """Return a datetime shifted by `years`, clamped to a valid date.

    Handles the leap-day case where Feb 29 has no equivalent in the target
    year by falling back to Feb 28. Time-of-day components are preserved.
    """
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:
        # Only possible when dt is Feb 29 and the target year is not a
        # leap year — fall back to Feb 28 of the target year.
        return dt.replace(year=dt.year + years, day=28)


def last_day_of_month(dt: datetime) -> datetime:
    """Return the last microsecond of dt's calendar month.

    Used as the upper bound when comparing partial-month data against the
    full reference month. The result is exclusive-friendly: any timestamp
    strictly less than this represents data inside the month.
    """
    if dt.month == 12:
        next_month = datetime(dt.year + 1, 1, 1)
    else:
        next_month = datetime(dt.year, dt.month + 1, 1)
    return next_month - timedelta(microseconds=1)
