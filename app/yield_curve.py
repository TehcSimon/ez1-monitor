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

With a weather forecast the same days also teach how many watts this plant
delivers per W/m² of forecast radiation at each time of day, which is what
turns the forecast into an expected curve. The sky comes from the forecast, the
plant's orientation and shading from its own history.

Measured on 139 real days, each estimated only from the days before it and
with the forecast as it was published 24 hours earlier. Median error of today's
total after 10 / 25 / 50 / 75 % of a clear day's energy:

    without a forecast   28 / 16 / 8 / 4 %
    with a forecast      14 / 12 / 7 / 4 %

A forecast also gives an estimate before the first watt (16 % median), and for
tomorrow it brings the 90th percentile of the error from 123 % (the average of
the last three weeks) down to 56 %.
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
# Radiation comes in hours, so the response is averaged over about one.
RESPONSE_SMOOTH_BINS = 6
# Radiation summed over the window: less than this says nothing about a slot.
MIN_RADIATION_SUM = 25
# Earlier than this share of the expected energy the estimate is mostly noise.
MIN_PROJECTION_SHARE = 0.1
DAY_DONE_SHARE = 0.99
# With a forecast, how much the day so far counts for the rest of it: the share
# of the expected energy already past, up to this. In the backtest the forecast
# was clearly better in the morning, the two about equal from midday on.
MAX_HISTORY_WEIGHT = 0.5


def standard_offset_seconds(year: int) -> int:
    """UTC offset of the server's timezone without daylight saving."""
    offsets = (datetime(year, month, 1).astimezone().utcoffset() for month in (1, 7))
    return int(min(o.total_seconds() for o in offsets))


def day_start_ts(day: date, offset_s: int) -> int:
    """Epoch of slot 0 of a day."""
    return calendar.timegm(day.timetuple()) - offset_s


def _smoothed(values: list, reach: int) -> list:
    out = []
    for b in range(len(values)):
        window = values[max(0, b - reach):b + reach + 1]
        out.append(sum(window) / len(window))
    return out


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
    return _smoothed(raw, SMOOTH_BINS)


def radiation_bins(hourly: dict, day_start: int) -> Optional[list]:
    """One day's radiation per slot, or None if the data doesn't cover it.

    hourly: {end of hour: mean W/m² of the hour before it}. Each value belongs
    to the middle of its hour; the slots between are interpolated.
    """
    slots = []
    for b in range(BINS):
        centre = day_start + b * BIN_SECONDS + BIN_SECONDS // 2
        hour = (centre + 1800) // 3600
        before, after = hourly.get(hour * 3600), hourly.get((hour + 1) * 3600)
        if before is None or after is None:
            return None
        fraction = (centre - (hour * 3600 - 1800)) / 3600
        slots.append(before + (after - before) * fraction)
    return slots


def learned_response(power_days: list, radiation_days: list) -> Optional[list]:
    """Watts per W/m² of radiation for every slot, or None with too few days.

    A ratio of sums over the window, so cloudy days count with what they
    delivered and a slot that is shaded every afternoon learns a low value.
    """
    if len(power_days) < MIN_LEARNED_DAYS:
        return None
    power = _smoothed([sum(day[b] for day in power_days) for b in range(BINS)], RESPONSE_SMOOTH_BINS)
    sky = _smoothed([sum(day[b] for day in radiation_days) for b in range(BINS)], RESPONSE_SMOOTH_BINS)
    return [p / s if s > MIN_RADIATION_SUM else 0.0 for p, s in zip(power, sky)]


def forecast_curve(response: list, radiation: list) -> list:
    """Expected watts per slot for a day with this forecast radiation."""
    return [r * g for r, g in zip(response, radiation)]


def curve_kwh(curve: list) -> float:
    return sum(curve) / BINS_PER_HOUR / 1000


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


def project_today(
    actual: list, target: Optional[list], now_bin: int, kwh_so_far: float,
    forecast: Optional[list] = None,
) -> dict:
    """Today's total: what the counter says plus an estimate of the rest.

    Only finished slots are compared. On top of kwh_so_far (the inverter's own
    counter, as in the figures above the day chart) comes the target that is
    still to come, scaled by actual / target so far. With a forecast curve that
    is blended with what the forecast expects for the rest of the day, the
    forecast counting for more the earlier it is.

    Returns {"kwh": None while it is too early to tell, "done": no target left}.
    """
    if not target:
        return {"kwh": None, "done": False}
    total = sum(target)
    planned = sum(target[:now_bin])
    if total <= 0:
        return {"kwh": None, "done": False}
    share = planned / total
    if forecast is None and share < MIN_PROJECTION_SHARE:
        return {"kwh": None, "done": False}
    ratio = sum(actual[:now_bin]) / planned if planned > 0 else 0.0
    rest_kwh = ratio * (total - planned) / BINS_PER_HOUR / 1000
    if forecast is not None:
        weight = min(MAX_HISTORY_WEIGHT, share)
        rest_kwh = weight * rest_kwh + (1 - weight) * curve_kwh(forecast[now_bin:])
    return {"kwh": round(kwh_so_far + rest_kwh, 3), "done": share >= DAY_DONE_SHARE}
