"""FastAPI app for EZ1 Monitor."""
import asyncio
import ipaddress
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, time
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import Database
from .poller import Poller

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ez1-monitor")


# --- Configuration validation --------------------------------------------

def _required_inverter_ip() -> str:
    """INVERTER_IP is mandatory and must look like a valid IP or hostname."""
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
    """Shift a datetime by N years.
    Falls back to Feb 28 for Feb 29 → non-leap-year edge cases."""
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:
        # Feb 29 in a non-leap target year
        return dt.replace(year=dt.year + years, day=28)


def _last_day_of_month(dt: datetime) -> datetime:
    """Return the very last microsecond of dt's calendar month."""
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

# Status classification thresholds
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
    """Classify the inverter's current state into one of four buckets."""
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    tz_name = os.environ.get("TZ", "system default")
    logger.info(
        f"Starting EZ1 Monitor — inverter at {INVERTER_IP}:{INVERTER_PORT}, "
        f"poll every {POLL_INTERVAL}s, currency={CURRENCY}, "
        f"price={PRICE_PER_KWH}/kWh, timezone={tz_name}, retention={RETENTION_DAYS}d"
    )
    await db.init()
    await poller.start()
    retention_handle = asyncio.create_task(retention_task())
    try:
        yield
    finally:
        logger.info("Shutting down ...")
        retention_handle.cancel()
        try:
            await retention_handle
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
):
    now = datetime.now()
    if range == "day":
        start = datetime.combine(now.date(), time.min)
        bucket = 0
    elif range == "week":
        start = now - timedelta(days=7)
        bucket = 600
    elif range == "month":
        start = now - timedelta(days=30)
        bucket = 3600
    else:
        start = now - timedelta(days=365)
        bucket = 86400
    points = await db.get_range(int(start.timestamp()), int(now.timestamp()), bucket)
    return {"range": range, "bucket_seconds": bucket, "points": points}


@app.get("/api/stats")
async def get_stats():
    """Aggregated statistics with period-over-period and year-over-year comparisons."""
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

    # Year-over-year reference points
    last_year_start = _shift_year(this_year_start, -1)
    last_year_until_today = _shift_year(now, -1)

    # "Same month last year" — two slices:
    #   - up to the same day-of-month as today (fair YoY % comparison)
    #   - full month (anchor reference)
    same_month_ly_start = _shift_year(this_month_start, -1)
    same_month_ly_until_today = _shift_year(now, -1)
    same_month_ly_full_end = _last_day_of_month(same_month_ly_start)

    async def energy_between(start: datetime, end: datetime) -> float:
        daily = await db.get_range(int(start.timestamp()), int(end.timestamp()), 86400)
        return sum(((d.get("e1") or 0) + (d.get("e2") or 0)) for d in daily)

    today_kwh = await energy_between(today_start, now)
    yesterday_kwh = await energy_between(yesterday_start, today_start)
    this_week_kwh = await energy_between(this_week_start, now)
    last_week_kwh = await energy_between(last_week_start, this_week_start)
    this_month_kwh = await energy_between(this_month_start, now)
    last_month_kwh = await energy_between(last_month_start, this_month_start)
    this_year_kwh = await energy_between(this_year_start, now)

    # Year-over-year aggregates
    last_year_ytd_kwh = await energy_between(last_year_start, last_year_until_today)
    same_month_ly_kwh = await energy_between(same_month_ly_start, same_month_ly_until_today)
    same_month_ly_total_kwh = await energy_between(same_month_ly_start, same_month_ly_full_end)

    total_kwh = await db.get_total_energy()
    co2_kg = total_kwh * CO2_KG_PER_KWH
    money_saved = total_kwh * PRICE_PER_KWH

    peak_w_today = await db.get_peak_today()
    daily_30d = await db.get_daily_totals(30)

    return {
        "today_kwh": round(today_kwh, 3),
        "yesterday_kwh": round(yesterday_kwh, 3),
        "this_week_kwh": round(this_week_kwh, 3),
        "last_week_kwh": round(last_week_kwh, 3),
        "this_month_kwh": round(this_month_kwh, 3),
        "last_month_kwh": round(last_month_kwh, 3),
        "this_year_kwh": round(this_year_kwh, 3),
        "last_year_ytd_kwh": round(last_year_ytd_kwh, 3),
        "same_month_last_year_kwh": round(same_month_ly_kwh, 3),
        "same_month_last_year_total_kwh": round(same_month_ly_total_kwh, 3),
        "same_month_last_year_iso": same_month_ly_start.strftime("%Y-%m"),
        "total_kwh": round(total_kwh, 3),
        "peak_w_today": round(peak_w_today, 1),
        "co2_saved_kg": round(co2_kg, 2),
        "money_saved": round(money_saved, 2),
        "daily_30d": daily_30d,
    }


# ---------------------------- Static frontend ----------------------------

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
