"""Per-panel figures of a single day, shown above the day curve.

Pure like money.py and date_helpers.py: it works on the raw rows of a day (see
Database.get_range), so the arithmetic is unit-testable without a database, and
the figures are derived from exactly the rows the curve below them is drawn
from.
"""

# What counts as producing, in watts of the whole array. Keeps a stray reading
# around dawn or dusk from stretching the production window.
PRODUCTION_THRESHOLD_W = 5


def _p1(point: dict) -> float:
    return point.get("p1") or 0


def _p2(point: dict) -> float:
    return point.get("p2") or 0


def _total(point: dict) -> float:
    return _p1(point) + _p2(point)


def _figures(points: list, power, kwh: float, hours: float) -> dict:
    """The figures of one row: its peak (the earliest one if it was reached
    more than once), and its average power over the production window."""
    peak = max(points, key=lambda p: (power(p), -p["timestamp"]), default=None)
    peak_w = power(peak) if peak else 0
    return {
        "peak_w": round(float(peak_w), 1),
        "peak_ts": peak["timestamp"] if peak_w > 0 else None,
        "avg_w": round(kwh * 1000.0 / hours, 1) if hours > 0 else None,
        "kwh": round(kwh, 3),
    }


def summarize_day(points: list) -> dict:
    """Figures per panel and for the whole array: {"pv1", "pv2", "total"}, each
    with peak_w, peak_ts (None without any power), avg_w and kwh.

    The production window, which avg_w refers to, is the same for all three:
    from the first to the last reading of the whole array above the threshold.
    A panel shaded for part of the day therefore shows a lower average instead
    of the same average over fewer hours, and the panels add up to the total.
    avg_w is None while that window is empty (no production yet, or a single
    reading so far).

    e1/e2 are the inverter's daily-resetting energy counters, so a panel's day
    total is the largest value seen, like in Database.get_today_panel_energy.
    """
    online = [p for p in points if p.get("online")]

    producing = [p["timestamp"] for p in online if _total(p) >= PRODUCTION_THRESHOLD_W]
    hours = (max(producing) - min(producing)) / 3600.0 if producing else 0.0

    kwh1 = float(max((p.get("e1") or 0 for p in online), default=0))
    kwh2 = float(max((p.get("e2") or 0 for p in online), default=0))

    return {
        "pv1": _figures(online, _p1, kwh1, hours),
        "pv2": _figures(online, _p2, kwh2, hours),
        "total": _figures(online, _total, kwh1 + kwh2, hours),
    }
