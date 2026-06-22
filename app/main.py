"""FastAPI app for EZ1 Monitor."""
import asyncio
import functools
import ipaddress
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import (
    CollectorRegistry, Gauge, Info, generate_latest, CONTENT_TYPE_LATEST,
)

from .database import Database
from .poller import Poller
from .co2 import CarbonState, resolve_current, poll_loop
from .date_helpers import shift_year, last_day_of_month
from . import __version__

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ez1-monitor")


# --- Configuration validation --------------------------------------------

def _required_inverter_ip() -> str:
    value = os.getenv("INVERTER_IP", "").strip()
    if not value:
        raise RuntimeError(
            "INVERTER_IP environment variable is required but not set. "
            "Set it to your EZ1-M's IP address (e.g. INVERTER_IP=192.168.50.123)."
        )
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if re.fullmatch(r"[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?", value):
        return value
    raise RuntimeError(
        f"INVERTER_IP='{value}' is not a valid IP address or hostname."
    )


# --- Configuration (from environment) -------------------------------------

INVERTER_IP = _required_inverter_ip()
INVERTER_PORT = int(os.getenv("INVERTER_PORT", "8050"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
DB_PATH = os.getenv("DB_PATH", "/data/ez1.db")
INSTALL_KWP = float(os.getenv("INSTALL_KWP", "1.0"))

RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "730"))

DEFAULT_LANG = os.getenv("DEFAULT_LANG", "").lower().strip()
SUPPORTED_LANGS = {"de", "en"}

CURRENCY = os.getenv("CURRENCY", "EUR").upper()
PRICE_PER_KWH = float(os.getenv("PRICE_PER_KWH", "0.35"))
CO2_KG_PER_KWH = float(os.getenv("CO2_KG_PER_KWH", "0.38"))

# Electricity Maps integration (optional)
ELECTRICITY_MAPS_TOKEN = os.getenv("ELECTRICITY_MAPS_TOKEN", "").strip()
# Note: The Home-Assistant-tier API doesn't take a zone parameter — the zone
# is bound to the token in the Electricity Maps portal. We still expose this
# env var for the UI display and for future endpoints, but it has no effect
# on the actual API call.
ELECTRICITY_MAPS_ZONE = os.getenv("ELECTRICITY_MAPS_ZONE", "DE").upper().strip()

ONLINE_FRESH_SECONDS = 300
DUSK_WINDOW_SECONDS = 300
DUSK_THRESHOLD_W = 5.0
# --------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"

db = Database(DB_PATH)

# Carbon-intensity state and resolver. Used by both the poller (stamps each
# measurement with the active factor) and the /api/live endpoint (reports
# the current factor and its provenance to the UI).
carbon_state = CarbonState(
    token=ELECTRICITY_MAPS_TOKEN,
    static_g_per_kwh=CO2_KG_PER_KWH * 1000.0,  # env is kg, internal is g
)

poller = Poller(INVERTER_IP, INVERTER_PORT, POLL_INTERVAL, db, carbon_state,
                price_per_kwh=PRICE_PER_KWH)


@functools.lru_cache(maxsize=128)
def _resolve_language(accept_language: str) -> str:
    """Map an Accept-Language header value to one of our supported langs.

    Cached because each header value is processed identically every time.
    The cache key is the raw header string, which is small and bounded
    (browsers don't send pathological values here). 128 entries comfortably
    covers any realistic user mix.
    """
    if accept_language:
        first = accept_language.split(",")[0].split(";")[0].strip().lower()
        primary = first.split("-")[0]
        if primary in SUPPORTED_LANGS:
            return primary
    return "en"


def detect_language(request: Request) -> str:
    if DEFAULT_LANG in SUPPORTED_LANGS:
        return DEFAULT_LANG
    return _resolve_language(request.headers.get("accept-language", ""))


async def compute_status() -> dict:
    latest = await db.get_latest()
    if not latest:
        return {"state": "noData", "age_seconds": None, "recent_avg_w": None}

    now_ts = int(datetime.now().timestamp())
    age = now_ts - (latest.get("timestamp") or now_ts)

    if latest.get("online") and age < ONLINE_FRESH_SECONDS:
        return {"state": "online", "age_seconds": age, "recent_avg_w": None}

    recent_avg = await db.get_recent_avg_power(DUSK_WINDOW_SECONDS)
    if recent_avg is None or recent_avg < DUSK_THRESHOLD_W:
        return {"state": "standby", "age_seconds": age, "recent_avg_w": recent_avg}
    return {"state": "error", "age_seconds": age, "recent_avg_w": recent_avg}


async def retention_task():
    if RETENTION_DAYS <= 0:
        logger.info("Retention disabled (RETENTION_DAYS <= 0)")
        return
    await asyncio.sleep(60)
    while True:
        try:
            deleted = await db.delete_old_measurements(RETENTION_DAYS)
            total = await db.count_measurements()
            if deleted > 0:
                logger.info(
                    f"Retention: pruned {deleted} measurements older than "
                    f"{RETENTION_DAYS} days. {total} rows remain."
                )
        except Exception as e:
            logger.warning(f"Retention task failed: {e}")
        await asyncio.sleep(86400)


async def backfill_aggregates() -> None:
    """One-time backfill of monthly and yearly aggregates from existing
    measurements. Idempotent — overwrites existing aggregate rows with
    freshly recomputed values.

    Walks from the earliest measurement to the current month, computing
    each month's aggregate. Then recomputes each year's aggregate from
    its monthly rows.

    IMPORTANT: months that start before the retention boundary are
    skipped. Their raw measurements have been (partially) pruned, so
    recomputing them from raw data would overwrite the stored complete
    aggregate with a reduced partial value — defeating the entire point
    of the long-term aggregate tables. Those months keep the value that
    was computed while their data was still complete.
    """
    earliest, latest = await db.get_measurements_date_range()
    if earliest is None:
        logger.info("Aggregate backfill: no measurements yet, skipping.")
        return

    start_date = datetime.fromtimestamp(earliest).date()
    end_date = datetime.fromtimestamp(latest).date()

    retention_cutoff = None
    if RETENTION_DAYS > 0:
        retention_cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).date()

    logger.info(
        f"Aggregate backfill: recomputing months from {start_date.isoformat()} "
        f"to {end_date.isoformat()}"
        + (f" (skipping months starting before {retention_cutoff.isoformat()})"
           if retention_cutoff else "")
    )

    current_year = start_date.year
    current_month = start_date.month
    years_touched = set()
    months_done = 0
    months_skipped = 0
    while (current_year, current_month) <= (end_date.year, end_date.month):
        month_start = datetime(current_year, current_month, 1).date()
        if retention_cutoff is not None and month_start < retention_cutoff:
            # Raw data for this month is no longer complete — keep the
            # frozen aggregate from when it was.
            months_skipped += 1
        else:
            await db.recompute_month_aggregate(current_year, current_month)
            years_touched.add(current_year)
            months_done += 1
        if current_month == 12:
            current_month = 1
            current_year += 1
        else:
            current_month += 1

    for year in sorted(years_touched):
        await db.recompute_year_aggregate(year)

    # Daily aggregates feed the Hall of Fame "best day" highscore. Walks
    # measurements once and writes one row per calendar day. The same
    # retention boundary is passed down so the day at the pruning edge
    # (whose intraday rows may be partially deleted) keeps its stored
    # complete aggregate — relevant for peak_w, which unlike the
    # cumulative e1/e2 counters does not survive partial pruning.
    daily_since = retention_cutoff.isoformat() if retention_cutoff else None
    daily_rows = await db.backfill_daily_aggregates(since_iso=daily_since)

    logger.info(
        f"Aggregate backfill: {months_done} months recomputed "
        f"({months_skipped} frozen outside retention), "
        f"{len(years_touched)} years, {daily_rows} days updated."
    )


async def aggregate_refresh_task():
    """Background task that refreshes the current month's aggregate plus
    today's daily aggregate every hour.

    On a month rollover it also recomputes the month that just ended,
    exactly once. Without this, the final hour(s) of a month between the
    last in-month refresh and midnight would only be captured on the next
    container restart (when backfill_aggregates re-walks everything). For
    a solar inverter that window is night-time and produces nothing, so
    the energy total is unaffected — but peak_w and the day/CO2/price
    weighting could in principle still move, and closing the gap keeps the
    frozen aggregate provably complete rather than "complete enough"."""
    await asyncio.sleep(300)  # let initial poll fill in first
    # Seed with the current month so the first loop iteration doesn't
    # mistake startup for a rollover (backfill already covered the past).
    last_refreshed_month: tuple[int, int] = (datetime.now().year, datetime.now().month)
    while True:
        try:
            now = datetime.now()
            this_month = (now.year, now.month)

            # Month rollover: finalize the month that just ended before we
            # move on to the new one. Respect the retention freeze — if the
            # ended month's raw data is already (partially) pruned, leave its
            # stored aggregate alone (same rule as backfill_aggregates).
            if this_month != last_refreshed_month:
                ended_year, ended_month = last_refreshed_month
                ended_start = datetime(ended_year, ended_month, 1).date()
                frozen = (
                    RETENTION_DAYS > 0
                    and ended_start < (now - timedelta(days=RETENTION_DAYS)).date()
                )
                if not frozen:
                    await db.recompute_month_aggregate(ended_year, ended_month)
                    await db.recompute_year_aggregate(ended_year)
                last_refreshed_month = this_month

            await db.recompute_month_aggregate(now.year, now.month)
            await db.recompute_year_aggregate(now.year)
            # Refresh daily aggregates. The full-window backfill is
            # idempotent and runs in <1s, so we just re-derive everything
            # within the retention window rather than maintain a separate
            # one-day codepath. Days at/behind the retention boundary stay
            # frozen (see backfill_aggregates for why).
            daily_since = None
            if RETENTION_DAYS > 0:
                daily_since = (now - timedelta(days=RETENTION_DAYS)).date().isoformat()
            await db.backfill_daily_aggregates(since_iso=daily_since)
        except Exception as e:
            logger.warning(f"Aggregate refresh task failed: {e}")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tz_name = os.environ.get("TZ", "system default")
    em_status = (
        f"Electricity Maps: enabled (zone {ELECTRICITY_MAPS_ZONE})"
        if ELECTRICITY_MAPS_TOKEN else
        f"Electricity Maps: disabled (static {CO2_KG_PER_KWH * 1000:.0f} g/kWh)"
    )
    logger.info(
        f"Starting EZ1 Monitor v{__version__} — inverter at {INVERTER_IP}:{INVERTER_PORT}, "
        f"poll every {POLL_INTERVAL}s, currency={CURRENCY}, "
        f"price={PRICE_PER_KWH}/kWh, timezone={tz_name}, retention={RETENTION_DAYS}d, "
        f"{em_status}"
    )
    await db.init()
    await backfill_aggregates()
    await poller.start()
    retention_handle = asyncio.create_task(retention_task())
    aggregate_handle = asyncio.create_task(aggregate_refresh_task())
    # CO2 polling task — runs forever, no-op when token isn't set
    carbon_handle = asyncio.create_task(poll_loop(carbon_state))
    try:
        yield
    finally:
        logger.info("Shutting down ...")
        for handle in (retention_handle, aggregate_handle, carbon_handle):
            handle.cancel()
            try:
                await handle
            except asyncio.CancelledError:
                pass
        await poller.stop()


app = FastAPI(title="EZ1 Monitor", lifespan=lifespan)


# ---------------------------- API ----------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/live")
async def get_live(request: Request):
    latest = await db.get_latest()
    info = await db.get_device_info()
    status = await compute_status()
    # Resolve current carbon-intensity factor + provenance for the UI.
    # The UI uses this to render the CO2-card subtitle (live/stale/avg/static).
    co2 = resolve_current(carbon_state)
    return {
        "latest": latest,
        "device": info,
        "status": status,
        "carbon": {
            "g_per_kwh": round(co2.g_per_kwh, 1),
            "source": co2.source,
            "datetime": co2.datetime,
            "fossil_pct": co2.fossil_pct,
            "country_code": co2.country_code,
            "age_seconds": co2.age_seconds,
            # Always echo back the static fallback so the UI can show it
            # in tooltips even when source="live"
            "static_g_per_kwh": carbon_state.static_g_per_kwh,
            # Echo configured zone for the UI label
            "configured_zone": ELECTRICITY_MAPS_ZONE if ELECTRICITY_MAPS_TOKEN else None,
            # How many successful polls have contributed to the rolling
            # average so far. Shown in the subtitle when source="avg".
            "rolling_count": carbon_state.rolling_count,
        },
        "config": {
            "version": __version__,
            "inverter_ip": INVERTER_IP,
            "poll_interval": POLL_INTERVAL,
            "install_kwp": INSTALL_KWP,
            "language": detect_language(request),
            "currency": CURRENCY,
            "price_per_kwh": PRICE_PER_KWH,
            "co2_kg_per_kwh": CO2_KG_PER_KWH,
            "retention_days": RETENTION_DAYS,
            "timezone": os.environ.get("TZ", "UTC"),
        },
    }


@app.get("/api/history")
async def get_history(
    range: str = Query("day", pattern="^(day|week|month|year|multiyear)$"),
    granularity: str = Query("auto", pattern="^(auto|daily|monthly|yearly)$"),
    mode: str = Query("rolling", pattern="^(rolling|calendar)$"),
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Historical data for the requested time range.

    Parameters:
    - range:       day | week | month | year | multiyear
    - granularity: only used with range=year (auto, daily, monthly) or
                   range=multiyear (monthly, yearly)
    - date:        only used with range=day; YYYY-MM-DD for a specific day

    Multi-year range pulls from the long-term aggregates tables, so it
    survives the RETENTION_DAYS pruning of detail measurements. Years
    where the inverter hasn't produced anything are auto-excluded by the
    underlying query.
    """
    now = datetime.now()

    # Multi-year view: aggregate tables (survives retention pruning)
    if range == "multiyear":
        if granularity == "yearly":
            # One bar per year
            yearly = await db.get_yearly_aggregates()
            return {
                "range": "multiyear",
                "granularity": "yearly",
                "years": yearly,
            }
        else:
            # Default: monthly granularity across all years with data.
            # The UI renders this as a continuous bar chart with year
            # boundary markers.
            monthly = await db.get_monthly_aggregates()
            return {
                "range": "multiyear",
                "granularity": "monthly",
                "months": monthly,
            }

    # Special path: monthly aggregate for the year view.
    if range == "year" and granularity == "monthly":
        if mode == "calendar":
            # Calendar year: the actual months Jan–Dec of the current year.
            # Returned in the same {month, kwh} shape as the rolling path;
            # the client pads any missing months out to a full Jan–Dec frame.
            rows = await db.get_monthly_aggregates(year=now.year)
            months = [
                {"month": f"{r['year']}-{r['month']:02d}", "kwh": r["total_kwh"]}
                for r in rows
            ]
            return {
                "range": "year", "granularity": "monthly", "mode": "calendar",
                "calendar_year": now.year, "months": months,
            }
        # Rolling: the last 12 months up to now.
        monthly = await db.get_monthly_totals(12)
        return {"range": "year", "granularity": "monthly", "mode": "rolling", "months": monthly}

    used_date: str | None = None
    # period_*_day frame the full calendar period for the client to pad the
    # chart with empty slots after "now" (only set in calendar mode). In
    # rolling mode the window simply trails "now" by a fixed span.
    period_start_day: str | None = None
    period_end_day: str | None = None

    if range == "day":
        # Optional ?date=YYYY-MM-DD for historical day lookups
        if date:
            try:
                target_date = datetime.strptime(date, "%Y-%m-%d").date()
            except ValueError:
                target_date = now.date()
            # Clamp to valid window: not in the future, not before retention
            today = now.date()
            if target_date > today:
                target_date = today
            if RETENTION_DAYS > 0:
                earliest = (now - timedelta(days=RETENTION_DAYS)).date()
                if target_date < earliest:
                    target_date = earliest
            start = datetime.combine(target_date, time.min)
            end = start + timedelta(days=1)
            used_date = target_date.strftime("%Y-%m-%d")
        else:
            start = datetime.combine(now.date(), time.min)
            end = now
        bucket = 0
    elif range == "week":
        bucket = 600
        if mode == "calendar":
            monday = datetime.combine(
                (now - timedelta(days=now.weekday())).date(), time.min
            )
            start, end = monday, now
            period_start_day = monday.date().isoformat()
            period_end_day = (monday.date() + timedelta(days=6)).isoformat()
        else:
            start, end = now - timedelta(days=7), now
    elif range == "month":
        bucket = 3600
        if mode == "calendar":
            first = datetime(now.year, now.month, 1)
            start, end = first, now
            period_start_day = first.date().isoformat()
            period_end_day = last_day_of_month(first).date().isoformat()
        else:
            start, end = now - timedelta(days=30), now
    else:  # year, daily
        bucket = 86400
        if mode == "calendar":
            start, end = datetime(now.year, 1, 1), now
            period_start_day = f"{now.year}-01-01"
            period_end_day = f"{now.year}-12-31"
        else:
            start, end = now - timedelta(days=365), now

    points = await db.get_range(int(start.timestamp()), int(end.timestamp()), bucket)
    return {
        "range": range,
        "granularity": "daily" if range == "year" else "auto",
        "mode": mode,
        "bucket_seconds": bucket,
        "date": used_date,
        "period_start_day": period_start_day,
        "period_end_day": period_end_day,
        "points": points,
    }


@app.get("/api/stats")
async def get_stats():
    """Aggregated statistics with same-period (calendar-aligned) comparisons."""
    now = datetime.now()
    today_start = datetime.combine(now.date(), time.min)
    yesterday_start = today_start - timedelta(days=1)
    this_week_start = today_start - timedelta(days=now.weekday())
    last_week_start = this_week_start - timedelta(days=7)
    this_month_start = datetime(now.year, now.month, 1)
    if now.month == 1:
        last_month_start = datetime(now.year - 1, 12, 1)
    else:
        last_month_start = datetime(now.year, now.month - 1, 1)
    this_year_start = datetime(now.year, 1, 1)

    # Same-period (calendar-aligned) reference points
    yesterday_until_now = now - timedelta(days=1)
    last_week_until_now = now - timedelta(days=7)
    last_month_until_progress = last_month_start + (now - this_month_start)
    # Edge case: last month may have fewer days than current progress
    last_month_end = last_day_of_month(last_month_start)
    if last_month_until_progress > last_month_end:
        last_month_until_progress = last_month_end

    # Year-over-year references
    last_year_start = shift_year(this_year_start, -1)
    last_year_until_today = shift_year(now, -1)
    same_month_ly_start = shift_year(this_month_start, -1)
    same_month_ly_until_today = shift_year(now, -1)
    same_month_ly_full_end = last_day_of_month(same_month_ly_start)

    # All energy windows we need for the four stat cards. Batched into a
    # single DB connection so we pay the connect cost once instead of 14
    # times. Order is significant — see the unpacking below.
    windows = [
        # Today card
        (today_start, now),                                           # this period
        (yesterday_start, yesterday_until_now),                       # same period yesterday
        (yesterday_start, today_start),                               # yesterday total
        # Week card
        (this_week_start, now),                                       # this period
        (last_week_start, last_week_until_now),                       # same period last week
        (last_week_start, this_week_start),                           # last week total
        # Month card (with year-over-year sub-block)
        (this_month_start, now),                                      # this period
        (last_month_start, last_month_until_progress),                # same period last month
        (last_month_start, this_month_start),                         # last month total
        (same_month_ly_start, same_month_ly_until_today),             # YoY same period
        (same_month_ly_start, same_month_ly_full_end),                # YoY full month
        # Year card
        (this_year_start, now),                                       # this period
        (last_year_start, last_year_until_today),                     # same period last year
        (last_year_start, this_year_start),                           # last year total
    ]
    window_ts = [(int(s.timestamp()), int(e.timestamp())) for s, e in windows]
    energies = await db.get_energy_in_windows(window_ts)

    (
        today_kwh,
        yesterday_until_now_kwh,
        yesterday_full_kwh,
        this_week_kwh,
        last_week_until_now_kwh,
        last_week_full_kwh,
        this_month_kwh,
        last_month_until_progress_kwh,
        last_month_full_kwh,
        same_month_ly_kwh,
        same_month_ly_total_kwh,
        this_year_kwh,
        last_year_ytd_kwh,
        last_year_full_kwh,
    ) = energies

    total_kwh = await db.get_total_energy()

    # CO2 saved and money saved: hybrid calculations that combine
    # historically-accurate stamped factors with a static fallback for the
    # portion of lifetime energy that predates the respective stamping
    # (CO2 since v1.4, price since v1.6.1) or came in during API outages.
    # Sourced from the long-term monthly aggregates (which survive
    # retention pruning) plus the current month live from measurements —
    # see Database.get_lifetime_factor_split. Over time the "measured"
    # shares grow and the totals naturally migrate from "static guess" to
    # "fully accurate", and they stay accurate after raw rows are pruned.
    split = await db.get_lifetime_factor_split(now.year, now.month)
    co2_kg = (
        split["co2_g"] / 1000.0
        + max(0.0, total_kwh - split["co2_kwh"]) * CO2_KG_PER_KWH
    )
    money_saved = (
        split["price_sum"]
        + max(0.0, total_kwh - split["price_kwh"]) * PRICE_PER_KWH
    )

    peak_w_today, peak_today_ts = await db.get_peak_today_with_time()
    pv1_kwh_today, pv2_kwh_today = await db.get_today_panel_energy()

    # Average power during today's production window. Uses the time between
    # first and last measurement with >=5 W output today. If there's no
    # production yet today (early morning, night), both fields are null.
    first_prod_ts, last_prod_ts = await db.get_today_production_window(threshold_w=5)
    if first_prod_ts is not None and last_prod_ts is not None and last_prod_ts > first_prod_ts:
        production_hours = (last_prod_ts - first_prod_ts) / 3600.0
        avg_w_during_production = (today_kwh * 1000.0) / production_hours if production_hours > 0 else None
    else:
        avg_w_during_production = None

    return {
        # Today
        "today_kwh": round(today_kwh, 3),
        "yesterday_until_now_kwh": round(yesterday_until_now_kwh, 3),
        "yesterday_full_kwh": round(yesterday_full_kwh, 3),

        # Week
        "this_week_kwh": round(this_week_kwh, 3),
        "last_week_until_now_kwh": round(last_week_until_now_kwh, 3),
        "last_week_full_kwh": round(last_week_full_kwh, 3),

        # Month
        "this_month_kwh": round(this_month_kwh, 3),
        "last_month_until_progress_kwh": round(last_month_until_progress_kwh, 3),
        "last_month_full_kwh": round(last_month_full_kwh, 3),

        # Year
        "this_year_kwh": round(this_year_kwh, 3),
        "last_year_ytd_kwh": round(last_year_ytd_kwh, 3),
        "last_year_full_kwh": round(last_year_full_kwh, 3),

        # Year-over-year (month card)
        "same_month_last_year_kwh": round(same_month_ly_kwh, 3),
        "same_month_last_year_total_kwh": round(same_month_ly_total_kwh, 3),
        "same_month_last_year_iso": same_month_ly_start.strftime("%Y-%m"),

        # Lifetime + peak
        "total_kwh": round(total_kwh, 3),
        "peak_w_today": round(peak_w_today, 1),
        "peak_today_ts": peak_today_ts,
        # Per-panel production today, DB-derived so it survives inverter
        # standby (the live reading is null at night).
        "pv1_kwh_today": round(pv1_kwh_today, 2),
        "pv2_kwh_today": round(pv2_kwh_today, 2),
        "avg_w_during_production": (
            round(avg_w_during_production, 1)
            if avg_w_during_production is not None else None
        ),
        "co2_saved_kg": round(co2_kg, 2),
        "money_saved": round(money_saved, 2),
    }


# ---------------------------- Hall of Fame ------------------------------

# Tier-unlock thresholds: how much comparison data we need before we
# consider a record "earned" enough to highlight. Below these thresholds
# the Hall of Fame still shows the current best value, but no animation
# fires — prevents fresh installs from blinking permanently for the first
# weeks while every new day is technically a record.
TIER_UNLOCK = {
    "day":   {"min_days": 14},                  # 2 weeks of comparison
    "week":  {"min_completed_weeks": 4},        # 4 finished ISO weeks
    "month": {"min_completed_months": 3},       # 3 finished months
    "year":  {"min_completed_years": 2},        # 2 finished calendar years
}

# How many days the record-glow animation stays active AFTER the day the
# record was set (the set day itself always shows as "fresh"). So day=1 means
# the record glows on the set day plus the one following day; year=7 means the
# set day plus the following week.
TIER_GLOW_DAYS = {
    "day":   1,
    "week":  2,
    "month": 3,
    "year":  7,
}


@app.get("/api/highscores")
async def get_highscores():
    """All-time best day, week, month and year for the Hall of Fame card.

    Each tier also carries a `state` field with one of:
      - "locked"  → not enough data yet, UI shows value but no animation
      - "fresh"   → record was set today, UI shows endless glow + NEW badge
      - "recent"  → record set within the tier's glow window, UI shows
                    one-time glow on page load
      - "settled" → record is older than the glow window, UI shows ruhe
    """
    extent = await db.get_data_extent()
    today = datetime.now().date()

    best_day = await db.get_best_day()
    best_week = await db.get_best_week()
    best_month = await db.get_best_month()
    best_year = await db.get_best_year()

    def day_state(record_date_iso: str | None, tier: str,
                  unlocked: bool) -> str:
        if not unlocked or record_date_iso is None:
            return "locked"
        try:
            record_date = datetime.strptime(record_date_iso, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return "settled"
        days_since = (today - record_date).days
        if days_since <= 0:
            return "fresh"
        if days_since <= TIER_GLOW_DAYS[tier]:
            return "recent"
        return "settled"

    def week_state(week_start_iso: str | None, iso_year: int, iso_week: int,
                   unlocked: bool) -> str:
        # The "set date" of a week record is the last day of that ISO week
        # (Sunday). We blink for tier_glow_days after the Sunday.
        if not unlocked or week_start_iso is None:
            return "locked"
        try:
            monday = datetime.strptime(week_start_iso, "%Y-%m-%d").date()
        except ValueError:
            return "settled"
        sunday = monday + timedelta(days=6)
        # Special case: if today is still inside the record week, "fresh"
        current_iy, current_iw, _ = today.isocalendar()
        if (current_iy, current_iw) == (iso_year, iso_week):
            return "fresh"
        days_since = (today - sunday).days
        if days_since <= 0:
            return "fresh"
        if days_since <= TIER_GLOW_DAYS["week"]:
            return "recent"
        return "settled"

    def month_state(year: int, month: int, unlocked: bool) -> str:
        if not unlocked:
            return "locked"
        if (today.year, today.month) == (year, month):
            return "fresh"
        # End of that month = day 1 of next month, minus 1
        if month == 12:
            next_month = datetime(year + 1, 1, 1).date()
        else:
            next_month = datetime(year, month + 1, 1).date()
        end_of_month = next_month - timedelta(days=1)
        days_since = (today - end_of_month).days
        if days_since <= 0:
            return "fresh"
        if days_since <= TIER_GLOW_DAYS["month"]:
            return "recent"
        return "settled"

    def year_state(year: int, unlocked: bool) -> str:
        if not unlocked:
            return "locked"
        if today.year == year:
            return "fresh"
        end_of_year = datetime(year, 12, 31).date()
        days_since = (today - end_of_year).days
        if days_since <= 0:
            return "fresh"
        if days_since <= TIER_GLOW_DAYS["year"]:
            return "recent"
        return "settled"

    day_unlocked = extent["days_with_data"] >= TIER_UNLOCK["day"]["min_days"]
    week_unlocked = extent["completed_weeks"] >= TIER_UNLOCK["week"]["min_completed_weeks"]
    month_unlocked = extent["completed_months"] >= TIER_UNLOCK["month"]["min_completed_months"]
    year_unlocked = extent["completed_years"] >= TIER_UNLOCK["year"]["min_completed_years"]

    return {
        "best_day": {
            "value": best_day,
            "state": day_state(best_day["date"] if best_day else None, "day",
                               day_unlocked),
        },
        "best_week": {
            "value": best_week,
            "state": (
                week_state(best_week["week_start"], best_week["iso_year"],
                           best_week["iso_week"], week_unlocked)
                if best_week else "locked"
            ),
        },
        "best_month": {
            "value": best_month,
            "state": (
                month_state(best_month["year"], best_month["month"],
                            month_unlocked)
                if best_month else "locked"
            ),
        },
        "best_year": {
            "value": best_year,
            "state": (
                year_state(best_year["year"], year_unlocked)
                if best_year else "locked"
            ),
        },
    }


# ---------------------------- Aggregates --------------------------------

@app.get("/api/aggregates")
async def get_aggregates(
    year: Optional[int] = Query(None, ge=2000, le=2999),
):
    """Long-term aggregate data that survives detail-data retention.

    Without parameters: returns all yearly aggregates.
    With year=YYYY:    returns monthly aggregates for that year.
    """
    if year is not None:
        monthly = await db.get_monthly_aggregates(year=year)
        return {"year": year, "monthly": monthly}
    yearly = await db.get_yearly_aggregates()
    return {"yearly": yearly}


# ---------------------------- Prometheus --------------------------------

# Use a custom registry so we don't pollute the default global one
# (which would inherit Python's process metrics — irrelevant here).
_metrics_registry = CollectorRegistry()

_m_current_power_w = Gauge("ez1_current_power_watts", "Current total power output", registry=_metrics_registry)
_m_pv1_power_w     = Gauge("ez1_pv1_power_watts", "PV1 current power", registry=_metrics_registry)
_m_pv2_power_w     = Gauge("ez1_pv2_power_watts", "PV2 current power", registry=_metrics_registry)
_m_today_kwh       = Gauge("ez1_today_kwh", "Energy produced today", registry=_metrics_registry)
_m_pv1_today_kwh   = Gauge("ez1_pv1_today_kwh", "PV1 energy today", registry=_metrics_registry)
_m_pv2_today_kwh   = Gauge("ez1_pv2_today_kwh", "PV2 energy today", registry=_metrics_registry)
_m_this_week_kwh   = Gauge("ez1_this_week_kwh", "Energy produced this week", registry=_metrics_registry)
_m_this_month_kwh  = Gauge("ez1_this_month_kwh", "Energy produced this month", registry=_metrics_registry)
_m_this_year_kwh   = Gauge("ez1_this_year_kwh", "Energy produced this year", registry=_metrics_registry)
_m_peak_today_w    = Gauge("ez1_peak_today_watts", "Peak power output today", registry=_metrics_registry)
_m_lifetime_kwh    = Gauge("ez1_lifetime_kwh_total", "Lifetime total energy", registry=_metrics_registry)
_m_co2_saved_kg    = Gauge("ez1_co2_saved_kg_total", "Lifetime CO2 avoided in kilograms", registry=_metrics_registry)
_m_status          = Gauge("ez1_status", "Inverter status (1 = active state)", ["state"], registry=_metrics_registry)
_m_info            = Info("ez1", "Inverter and monitor metadata", registry=_metrics_registry)

# Carbon-intensity gauges. Always exported, but the values are static
# (CO2_KG_PER_KWH × 1000) when no Electricity Maps token is configured.
_m_co2_intensity   = Gauge("ez1_carbon_intensity_g_per_kwh", "Current grid carbon intensity in gCO2eq/kWh", registry=_metrics_registry)
_m_co2_fossil_pct  = Gauge("ez1_carbon_fossil_percentage", "Percentage of grid electricity from fossil fuels", registry=_metrics_registry)
_m_co2_source      = Gauge("ez1_carbon_source", "Carbon intensity data source (1 = active)", ["source"], registry=_metrics_registry)


async def _populate_metrics() -> None:
    """Refresh all Prometheus gauges from current state. Called on every
    scrape so we always serve the freshest values without needing a
    background updater."""
    latest = await db.get_latest()
    if latest:
        if latest.get("online"):
            p1 = latest.get("p1") or 0
            p2 = latest.get("p2") or 0
            _m_current_power_w.set(p1 + p2)
            _m_pv1_power_w.set(p1)
            _m_pv2_power_w.set(p2)
            _m_pv1_today_kwh.set(latest.get("e1") or 0)
            _m_pv2_today_kwh.set(latest.get("e2") or 0)
        else:
            _m_current_power_w.set(0)
            _m_pv1_power_w.set(0)
            _m_pv2_power_w.set(0)

    status = await compute_status()
    for state in ("online", "standby", "error", "noData"):
        _m_status.labels(state=state).set(1 if status["state"] == state else 0)

    stats = await get_stats()
    _m_today_kwh.set(stats.get("today_kwh") or 0)
    _m_this_week_kwh.set(stats.get("this_week_kwh") or 0)
    _m_this_month_kwh.set(stats.get("this_month_kwh") or 0)
    _m_this_year_kwh.set(stats.get("this_year_kwh") or 0)
    _m_peak_today_w.set(stats.get("peak_w_today") or 0)
    _m_lifetime_kwh.set(stats.get("total_kwh") or 0)
    _m_co2_saved_kg.set(stats.get("co2_saved_kg") or 0)

    # Carbon intensity metrics
    co2 = resolve_current(carbon_state)
    _m_co2_intensity.set(co2.g_per_kwh)
    if co2.fossil_pct is not None:
        _m_co2_fossil_pct.set(co2.fossil_pct)
    for src in ("live", "stale", "avg", "static"):
        _m_co2_source.labels(source=src).set(1 if co2.source == src else 0)

    info = await db.get_device_info()
    if info:
        _m_info.info({
            "device_id": str(info.get("device_id") or ""),
            "firmware": str(info.get("firmware") or ""),
            "max_power": str(info.get("max_power") or ""),
            "version": __version__,
        })
    else:
        _m_info.info({"version": __version__})


# Scrape-side TTL cache: _populate_metrics runs ~15 queries (including the
# full stats computation), which adds up with aggressive Prometheus scrape
# intervals on NAS-grade hardware. Gauges keep their last values between
# refreshes, so serving a snapshot up to 30s old is harmless for data that
# changes once per POLL_INTERVAL anyway.
METRICS_CACHE_TTL_S = 30
_metrics_last_populated: float = float("-inf")


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint. No authentication; expected to be
    accessed from within the LAN only."""
    global _metrics_last_populated
    now_mono = asyncio.get_running_loop().time()
    if now_mono - _metrics_last_populated >= METRICS_CACHE_TTL_S:
        await _populate_metrics()
        _metrics_last_populated = now_mono
    return Response(generate_latest(_metrics_registry), media_type=CONTENT_TYPE_LATEST)


# ---------------------------- Static frontend ----------------------------

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
