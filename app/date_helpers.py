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


def same_progress_slice(kind: str, anchored_start: date_cls, today: date_cls):
    """Date ranges to compare an anchored past week/month against the
    currently RUNNING week/month at equal progress ("record pace").

    kind is "week" (anchored_start = that week's Monday) or "month"
    (anchored_start = the 1st of that month). Returns a pair of inclusive
    date ranges:

        ((slice_start, slice_end), (current_start, current_end))

    where the first range is the anchored period cut down to the running
    period's COMPLETED days, and the second is the running period up to
    and including yesterday.

    Completed days only — the started "today" is deliberately excluded
    from BOTH sides: comparing a few morning hours against the anchored
    period's full day read "+2000 %" for the record at breakfast. So on a
    Tuesday the comparison is Monday-vs-Monday (both complete); on Monday
    (resp. the 1st) there is no fair day-granular basis yet and the
    function returns None, as it does when the anchored period IS the
    running one. The slice is clamped to the anchored period's length
    (February vs. the 30th of the running month yields all of February).

    Day-granular by design: the pace pill sums daily_aggregates rows, and
    anything finer would suggest a precision the hourly-refreshed daily
    rows don't have.
    """
    if kind == "week":
        iso_year, iso_week, _ = today.isocalendar()
        current_start = iso_week_monday(iso_year, iso_week)
        if anchored_start == current_start:
            return None
        completed_days = today.isoweekday() - 1
        if completed_days <= 0:
            return None
        slice_end = anchored_start + timedelta(days=completed_days - 1)
    else:  # "month"
        current_start = today.replace(day=1)
        if anchored_start == current_start:
            return None
        completed_days = today.day - 1
        if completed_days <= 0:
            return None
        anchored_last = last_day_of_month(
            datetime(anchored_start.year, anchored_start.month, 1)
        ).date()
        slice_end = anchored_start + timedelta(days=completed_days - 1)
        if slice_end > anchored_last:
            slice_end = anchored_last
    return ((anchored_start, slice_end),
            (current_start, today - timedelta(days=1)))
