"""Date math helpers for stat aggregations.

Pure functions, extracted into their own module so they can be unit-tested
without importing the full FastAPI app (which initializes the database,
poller, and HTTP client at import time).
"""
from datetime import datetime, timedelta


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
