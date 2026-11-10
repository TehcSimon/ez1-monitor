"""Target vs. actual for one day, and today's projection.

Pure like day_figures.py and money.py. The target is what a clear day has
recently delivered at this time of day: for every 5-minute slot the 90th
percentile of the last three weeks, smoothed a little. It is learned from the
plant's own history, so it needs no module data and already contains recurring
shading (a privacy screen, a neighbouring roof).

Slots count from midnight in the timezone's *standard* time, all year round.
Local clock time would shift the learned curve by an hour at every clock
change. The slot frame is kept per day, so a production day is never cut in
two, which plain UTC days would do outside Europe.

Measured on 139 real days, each estimated only from the days before it: the
projection's median error is 16 / 8 / 4 % after 25 / 50 / 75 % of a clear
day's energy.
"""
import calendar
from datetime import date, datetime
from typing import Optional

BIN_SECONDS = 300
BINS = 86400 // BIN_SECONDS
BINS_PER_HOUR = 3600 / BIN_SECONDS

WINDOW_DAYS = 21
# Fewer days than this and the percentile is little more than the best day.
MIN_LEARNED_DAYS = 7
PERCENTILE = 0.9
SMOOTH_BINS = 2
# Earlier than this share of the expected energy the estimate is mostly noise.
MIN_PROJECTION_SHARE = 0.1
DAY_DONE_SHARE = 0.99


def standard_offset_seconds(year: int) -> int:
    """UTC offset of the server's timezone without daylight saving."""
    offsets = (datetime(year, month, 1).astimezone().utcoffset() for month in (1, 7))
    return int(min(o.total_seconds() for o in offsets))


def day_start_ts(day: date, offset_s: int) -> int:
    """Epoch of slot 0 of a day."""
    return calendar.timegm(day.timetuple()) - offset_s


def learned_target(days: list) -> Optional[list]:
    """The target curve in watts, or None while there are too few days.

    days: one list of BINS watt values per recorded day.
    """
    if len(days) < MIN_LEARNED_DAYS:
        return None
    raw = []
    for b in range(BINS):
        values = sorted(day[b] for day in days)
        raw.append(values[min(len(values) - 1, int(PERCENTILE * len(values)))])
    smooth = []
    for b in range(BINS):
        window = raw[max(0, b - SMOOTH_BINS):b + SMOOTH_BINS + 1]
        smooth.append(sum(window) / len(window))
    return smooth


def bins_of_day(points: list, day_start: int) -> tuple:
    """Average p1 and p2 per slot from the raw rows of a day (Database.get_range)."""
    sums1, sums2, counts = [0.0] * BINS, [0.0] * BINS, [0] * BINS
    for point in points:
        if not point.get("online"):
            continue
        b = (point["timestamp"] - day_start) // BIN_SECONDS
        if 0 <= b < BINS:
            sums1[b] += point.get("p1") or 0
            sums2[b] += point.get("p2") or 0
            counts[b] += 1
    return (
        [s / c if c else 0.0 for s, c in zip(sums1, counts)],
        [s / c if c else 0.0 for s, c in zip(sums2, counts)],
    )


def project_today(actual: list, target: Optional[list], now_bin: int, kwh_so_far: float) -> dict:
    """Today's total if the rest of the day goes like it has gone so far.

    Only finished slots are compared. What is added on top of kwh_so_far (the
    inverter's own counter, as in the figures above the day chart) is the
    target that is still to come, scaled by actual / target so far.

    Returns {"kwh": None while it is too early to tell, "done": no target left}.
    """
    if not target:
        return {"kwh": None, "done": False}
    total = sum(target)
    planned = sum(target[:now_bin])
    if total <= 0:
        return {"kwh": None, "done": False}
    share = planned / total
    if share < MIN_PROJECTION_SHARE:
        return {"kwh": None, "done": False}
    ratio = sum(actual[:now_bin]) / planned
    remaining_kwh = (total - planned) / BINS_PER_HOUR / 1000
    return {"kwh": round(kwh_so_far + ratio * remaining_kwh, 3), "done": share >= DAY_DONE_SHARE}
