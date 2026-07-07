"""Pure money-model helpers.

Kept free of FastAPI / DB / inverter-library imports (like date_helpers.py)
so the arithmetic is trivially unit-testable on any Python without the
runtime dependencies.
"""
from datetime import date, timedelta
from math import ceil
from typing import Optional


def compute_money_saved(
    money_saved_full: float, total_kwh: float,
    self_consumption_pct: float, feed_in_tariff: float,
) -> float:
    """Realistic money saved, derived from the 100%-self-consumption figure.

    Only the self-consumed share offsets the retail price (that price is
    already baked into money_saved_full); the fed-in remainder earns
    feed_in_tariff. self_consumption_pct=100 reproduces money_saved_full
    exactly, so the default config is byte-for-byte the pre-v1.8 behaviour.
    """
    scq = max(0.0, min(100.0, self_consumption_pct)) / 100.0
    return scq * money_saved_full + (1.0 - scq) * feed_in_tariff * total_kwh


def estimate_breakeven_date(
    install_cost: float, money_saved: float,
    first_data_date: date, today: date, min_days: int = 365,
) -> Optional[date]:
    """Projected calendar date on which cumulative savings reach
    install_cost, linearly extrapolated from the average savings rate over
    the WHOLE data history.

    Deliberately naive-but-honest: the rate is money_saved divided by the
    CALENDAR days since the first measurement — not days-with-data. Gaps
    (outages, snow) produce nothing and must drag the rate down, otherwise
    the projected date would be too optimistic.

    Gated behind min_days (default 365): with less than a full year of
    data the rate is seasonally biased — a summer-only history predicts a
    far too early date — so we show nothing rather than nonsense. Same
    philosophy as the Hall-of-Fame tier unlocks.

    Returns a date, or None when not applicable: no install cost, nothing
    saved yet, already amortized (the real break-even date exists then),
    or not enough history.
    """
    if install_cost <= 0 or money_saved <= 0 or money_saved >= install_cost:
        return None
    span_days = (today - first_data_date).days
    if span_days < min_days:
        return None
    rate_per_day = money_saved / span_days
    remaining_days = ceil((install_cost - money_saved) / rate_per_day)
    return today + timedelta(days=remaining_days)
