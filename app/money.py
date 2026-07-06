"""Pure money-model helpers.

Kept free of FastAPI / DB / inverter-library imports (like date_helpers.py)
so the arithmetic is trivially unit-testable on any Python without the
runtime dependencies.
"""


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
