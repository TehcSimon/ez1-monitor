"""Lifetime energy that survives an inverter counter falling back.

The inverter's te1/te2 counters are the source of the lifetime figure, and
with it of the money, CO2 and payback figures built on top. Known firmware
wraps each of them to 0 near 540 kWh, a replaced inverter starts over, and a
power cut can cost a counter part of what it had. Taken at face value, that
would take the history with it.

The input is one counter's readings over time: each past day's highest value
(kept in daily_aggregates, so it survives retention pruning), today's highest
value and the latest reading. The correction is derived from them on every
read and never stored, so a wrong decision can't pile up in the database, and
a counter that never falls comes out as exactly its latest reading.

When in doubt this leaves the reading alone, which is what the monitor did
before it knew about falling counters at all.
"""

# Changes smaller than this (kWh) are noise, not a counter falling back.
TOLERANCE_KWH = 1.0

# A fall this large is the firmware's overflow near 540 kWh. Anything less is
# some other partial loss, and the two are added back differently, see below.
OVERFLOW_FALL_KWH = 500.0

# How many following readings (at most one per day, so at least this many
# days) may prove a fall or a jump to be a glitch: back at the old level this
# soon means the counter never really left it. A real wrap can't do that, even
# at 900 W per input it takes well over three weeks to get back to 540 kWh.
GLITCH_WINDOW = 14


def corrected_counter(readings: list[float]) -> float:
    """One counter's lifetime value, with every real fall added back.

    readings are in chronological order, the latest reading last.
    """
    added_back = 0.0
    current = None
    for i, value in enumerate(readings):
        ahead = readings[i + 1:i + 1 + GLITCH_WINDOW]
        if current is None:
            # Nothing to compare the first reading with. If the very next one
            # is lower, a jump is as likely as a wrap: don't build on it.
            if not ahead or ahead[0] >= value - TOLERANCE_KWH:
                current = value
        elif value < current - TOLERANCE_KWH:
            # The latest reading has nothing to confirm it yet. Holding the
            # old level until tomorrow's reading costs a few hours of a
            # wrap day, and makes a single bad poll entirely harmless.
            if not ahead or any(later >= current - TOLERANCE_KWH for later in ahead):
                continue
            # An overflow restarted the counter from 0, so all of `value` is
            # new energy on top of the old level. After any other fall only
            # the difference is known to be lost: adding that back leaves the
            # total where it was, so a fall of this kind can never inflate it.
            fall = current - value
            added_back += current if fall >= OVERFLOW_FALL_KWH else fall
            current = value
        elif value > current + TOLERANCE_KWH and any(
            current - TOLERANCE_KWH <= later < value - TOLERANCE_KWH for later in ahead
        ):
            # A jump up, followed by readings that carry on from before it.
            continue
        else:
            current = value
    return added_back + (current if current is not None else 0.0)
