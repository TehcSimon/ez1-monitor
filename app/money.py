"""Pure money-model helpers.

Kept free of FastAPI / DB / inverter-library imports (like date_helpers.py)
so the arithmetic is trivially unit-testable on any Python without the
runtime dependencies.
"""
from datetime import date, timedelta
from math import ceil
from typing import Optional


def compute_money_saved_full(
    price_sum: float, price_kwh: float, total_kwh: float,
    oldest_price: Optional[float], current_price: float,
) -> float:
    """Lifetime money saved at 100% self-consumption.

    price_sum is the value of the price_kwh that carry a stamped price (see
    Database.get_lifetime_factor_split). The rest of the inverter's lifetime
    counter has no price of its own: production from before the monitor was
    set up or before stamping existed (v1.6.1), and whole days the monitor
    was down. It is valued at oldest_price, the earliest stamped price:
    almost all of that energy predates the first stamped reading, so this is
    the best estimate we have, and unlike the current price it does not move
    when the tariff changes. Valuing it at the current price re-priced that
    energy on every tariff change, making the card jump although nothing had
    been produced. Only while no month carries a stamped price yet (a fresh
    install) does it fall back to current_price.
    """
    remainder_price = current_price if oldest_price is None else oldest_price
    return price_sum + max(0.0, total_kwh - price_kwh) * remainder_price


def average_price_per_kwh(
    money_saved_full: float, total_kwh: float, price_kwh: float,
) -> Optional[float]:
    """Average retail price behind money_saved_full, None without any energy.

    The basis is the one compute_money_saved_full valued: the lifetime
    counter, or the stamped energy where rounding put that slightly above it.
    """
    basis = max(total_kwh, price_kwh)
    return money_saved_full / basis if basis > 0 else None


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


def value_of_new_kwh(
    current_price: float, self_consumption_pct: float, feed_in_tariff: float,
) -> float:
    """What one kWh produced from now on is worth under the current config:
    the money model above, applied to a single kWh at the current price."""
    return compute_money_saved(current_price, 1.0, self_consumption_pct, feed_in_tariff)


def estimate_breakeven_date(
    install_cost: float, money_saved: float,
    first_data_date: date, today: date,
    recorded_kwh: float, value_per_kwh: float, min_days: int = 365,
) -> Optional[date]:
    """Projected calendar date on which cumulative savings reach
    install_cost, linearly extrapolated from the average production over the
    WHOLE data history, valued at what a kWh is worth today.

    Deliberately naive-but-honest: the production rate is the energy recorded
    since the first measurement divided by the CALENDAR days since then — not
    days-with-data. Gaps (outages, snow) produce nothing and must drag the
    rate down, otherwise the projected date would be too optimistic.

    recorded_kwh is only what was recorded between first_data_date and today.
    money_saved is valued from the inverter's lifetime counter, so deriving
    the rate from it would divide production from before the monitor was set
    up by the monitor's shorter history and project break-even far too early.

    value_per_kwh is the value of a kWh produced from now on
    (value_of_new_kwh), because that is what the remaining production will
    earn. The money saved so far already reflects past tariffs; extrapolating
    its average would keep promising the old price after a tariff change.

    Gated behind min_days (default 365): with less than a full year of
    data the rate is seasonally biased — a summer-only history predicts a
    far too early date — so we show nothing rather than nonsense. Same
    philosophy as the Hall-of-Fame tier unlocks.

    Returns a date, or None when not applicable: no install cost, nothing
    saved yet, already amortized (the real break-even date exists then),
    not enough history, or no savings rate to extrapolate.
    """
    if install_cost <= 0 or money_saved <= 0 or money_saved >= install_cost:
        return None
    span_days = (today - first_data_date).days
    if span_days < min_days:
        return None
    rate_per_day = recorded_kwh / span_days * value_per_kwh
    if rate_per_day <= 0:
        return None
    remaining_days = ceil((install_cost - money_saved) / rate_per_day)
    return today + timedelta(days=remaining_days)
