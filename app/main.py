"""FastAPI app for EZ1 Monitor."""
import asyncio
import ipaddress
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import Database
from .poller import Poller
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


def _shift_year(dt: datetime, years: int = -1) -> datetime:
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:
        return dt.replace(year=dt.year + years, day=28)


def _last_day_of_month(dt: datetime) -> datetime:
    if dt.month == 12:
        next_month = datetime(dt.year + 1, 1, 1)
    else:
        next_month = datetime(dt.year, dt.month + 1, 1)
    return next_month - timedelta(microseconds=1)


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

ONLINE_FRESH_SECONDS = 300
DUSK_WINDOW_SECONDS = 300
DUSK_THRESHOLD_W = 5.0
# --------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"

db = Database(DB_PATH)
poller = Poller(INVERTER_IP, INVERTER_PORT, POLL_INTERVAL, db)


def detect_language(request: Request) -> str:
    if DEFAULT_LANG in SUPPORTED_LANGS:
        return DEFAULT_LANG
    accept = request.headers.get("accept-language", "")
    if accept:
        first = accept.split(",")[0].split(";")[0].strip().lower()
        primary = first.split("-")[0]
        if primary in SUPPORTED_LANGS:
            return primary
    return "en"


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
    """
    earliest, latest = await db.get_measurements_date_range()
    if earliest is None:
        logger.info("Aggregate backfill: no measurements yet, skipping.")
        return

    start_date = datetime.fromtimestamp(earliest).date()
    end_date = datetime.fromtimestamp(latest).date()
    logger.info(
        f"Aggregate backfill: recomputing months from {start_date.isoformat()} "
        f"to {end_date.isoformat()}"
    )

    current_year = start_date.year
    current_month = start_date.month
    years_touched = set()
    months_done = 0
    while (current_year, current_month) <= (end_date.year, end_date.month):
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

    logger.info(
        f"Aggregate backfill: {months_done} months, {len(years_touched)} years updated."
    )


async def aggregate_refresh_task():
    """Background task that refreshes the current month's aggregate every
    hour. Past months stay frozen unless the container is restarted and the
    backfill picks them up again."""
    await asyncio.sleep(300)  # let initial poll fill in first
    while True:
        try:
            now = datetime.now()
            await db.recompute_month_aggregate(now.year, now.month)
            await db.recompute_year_aggregate(now.year)
        except Exception as e:
            logger.warning(f"Aggregate refresh task failed: {e}")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tz_name = os.environ.get("TZ", "system default")
    logger.info(
        f"Starting EZ1 Monitor v{__version__} — inverter at {INVERTER_IP}:{INVERTER_PORT}, "
        f"poll every {POLL_INTERVAL}s, currency={CURRENCY}, "
        f"price={PRICE_PER_KWH}/kWh, timezone={tz_name}, retention={RETENTION_DAYS}d"
    )
    await db.init()
    await backfill_aggregates()
    await poller.start()
    retention_handle = asyncio.create_task(retention_task())
    aggregate_handle = asyncio.create_task(aggregate_refresh_task())
    try:
        yield
    finally:
        logger.info("Shutting down ...")
        for handle in (retention_handle, aggregate_handle):
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
    return {
        "latest": latest,
        "device": info,
        "status": status,
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
    range: str = Query("day", pattern="^(day|week|month|year)$"),
    granularity: str = Query("auto", pattern="^(auto|daily|monthly)$"),
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Historical data for the requested time range.

    Parameters:
    - range:       day | week | month | year
    - granularity: only used with range=year; daily (default) or monthly
    - date:        only used with range=day; YYYY-MM-DD for a specific day
                   (default: today). Must not be in the future and not older
                   than RETENTION_DAYS.
    """
    now = datetime.now()

    # Special path: rolling 12-month aggregate for the year view
    if range == "year" and granularity == "monthly":
        monthly = await db.get_monthly_totals(12)
        return {"range": "year", "granularity": "monthly", "months": monthly}

    if range == "day":
        # Optional ?date=YYYY-MM-DD for historical day lookups
        used_date = None
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
        start = now - timedelta(days=7)
        end = now
        bucket = 600
    elif range == "month":
        start = now - timedelta(days=30)
        end = now
        bucket = 3600
    else:  # year, daily
        start = now - timedelta(days=365)
        end = now
        bucket = 86400

    points = await db.get_range(int(start.timestamp()), int(end.timestamp()), bucket)
    return {
        "range": range,
        "granularity": "daily" if range == "year" else "auto",
        "bucket_seconds": bucket,
        "date": used_date if range == "day" else None,
        "points": points,
    }


@app.get("/api/stats")
async def get_stats():
    """Aggregated statistics with stichtag (same-progress) comparisons."""
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

    # Stichtag (same-progress) reference points
    yesterday_until_now = now - timedelta(days=1)
    last_week_until_now = now - timedelta(days=7)
    last_month_until_progress = _shift_year(
        last_month_start, 0
    ) + (now - this_month_start)
    # Edge case: last month may have fewer days than current progress
    last_month_end = _last_day_of_month(last_month_start)
    if last_month_until_progress > last_month_end:
        last_month_until_progress = last_month_end

    # Year-over-year references
    last_year_start = _shift_year(this_year_start, -1)
    last_year_until_today = _shift_year(now, -1)
    same_month_ly_start = _shift_year(this_month_start, -1)
    same_month_ly_until_today = _shift_year(now, -1)
    same_month_ly_full_end = _last_day_of_month(same_month_ly_start)

    async def energy_between(start: datetime, end: datetime) -> float:
        daily = await db.get_range(int(start.timestamp()), int(end.timestamp()), 86400)
        return sum(((d.get("e1") or 0) + (d.get("e2") or 0)) for d in daily)

    # Today / yesterday
    today_kwh = await energy_between(today_start, now)
    yesterday_until_now_kwh = await energy_between(yesterday_start, yesterday_until_now)
    yesterday_full_kwh = await energy_between(yesterday_start, today_start)

    # Week
    this_week_kwh = await energy_between(this_week_start, now)
    last_week_until_now_kwh = await energy_between(last_week_start, last_week_until_now)
    last_week_full_kwh = await energy_between(last_week_start, this_week_start)

    # Month (same calendar progress + full)
    this_month_kwh = await energy_between(this_month_start, now)
    last_month_until_progress_kwh = await energy_between(
        last_month_start, last_month_until_progress
    )
    last_month_full_kwh = await energy_between(last_month_start, this_month_start)

    # Year
    this_year_kwh = await energy_between(this_year_start, now)
    last_year_ytd_kwh = await energy_between(last_year_start, last_year_until_today)
    # End of last year = start of this year (exclusive boundary)
    last_year_full_kwh = await energy_between(last_year_start, this_year_start)

    # Year-over-year on month card
    same_month_ly_kwh = await energy_between(same_month_ly_start, same_month_ly_until_today)
    same_month_ly_total_kwh = await energy_between(same_month_ly_start, same_month_ly_full_end)

    total_kwh = await db.get_total_energy()
    co2_kg = total_kwh * CO2_KG_PER_KWH
    money_saved = total_kwh * PRICE_PER_KWH
    peak_w_today = await db.get_peak_today()

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
        "co2_saved_kg": round(co2_kg, 2),
        "money_saved": round(money_saved, 2),
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

from prometheus_client import (
    CollectorRegistry, Gauge, Info, generate_latest, CONTENT_TYPE_LATEST,
)
from fastapi.responses import Response

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

    info = await db.get_device_info()
    if info:
        _m_info.info({
            "device_id": str(info.get("device_id") or ""),
            "serial_number": str(info.get("serial_number") or ""),
            "max_power": str(info.get("max_power") or ""),
            "version": __version__,
        })
    else:
        _m_info.info({"version": __version__})


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint. No authentication; expected to be
    accessed from within the LAN only."""
    await _populate_metrics()
    return Response(generate_latest(_metrics_registry), media_type=CONTENT_TYPE_LATEST)


# ---------------------------- Static frontend ----------------------------

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
