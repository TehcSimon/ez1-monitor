"""FastAPI app for EZ1 Monitor."""
import asyncio
import logging
import os
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

# --- Configuration (from environment) -------------------------------------
INVERTER_IP = os.getenv("INVERTER_IP", "192.168.1.194")
INVERTER_PORT = int(os.getenv("INVERTER_PORT", "8050"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
DB_PATH = os.getenv("DB_PATH", "/data/ez1.db")
INSTALL_KWP = float(os.getenv("INSTALL_KWP", "1.0"))

# Data retention: keep measurements for this many days. Default: 2 years
# (allows year-over-year comparisons). Set to 0 to disable automatic pruning.
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "730"))

# Localization
# DEFAULT_LANG: empty string = auto-detect from browser Accept-Language header
#               "de" / "en"   = force this language for all clients
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "").lower().strip()
SUPPORTED_LANGS = {"de", "en"}

# Economic / environmental values for lifetime savings display
CURRENCY = os.getenv("CURRENCY", "EUR").upper()
PRICE_PER_KWH = float(os.getenv("PRICE_PER_KWH", "0.35"))
CO2_KG_PER_KWH = float(os.getenv("CO2_KG_PER_KWH", "0.38"))
# --------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"

db = Database(DB_PATH)
poller = Poller(INVERTER_IP, INVERTER_PORT, POLL_INTERVAL, db)


def detect_language(request: Request) -> str:
    """Determine UI language: env override > browser Accept-Language > 'en' fallback."""
    if DEFAULT_LANG in SUPPORTED_LANGS:
        return DEFAULT_LANG

    accept = request.headers.get("accept-language", "")
    if accept:
        first = accept.split(",")[0].split(";")[0].strip().lower()
        primary = first.split("-")[0]
        if primary in SUPPORTED_LANGS:
            return primary

    return "en"


async def retention_task():
    """Background task: prune measurements older than RETENTION_DAYS, once per day."""
    if RETENTION_DAYS <= 0:
        logger.info("Retention disabled (RETENTION_DAYS <= 0)")
        return

    # Wait a bit before first run so the DB has time to settle on startup
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
        await asyncio.sleep(86400)  # run once per day


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
    """Simple health endpoint for container health checks."""
    return {"status": "ok"}


@app.get("/api/live")
async def get_live(request: Request):
    """Latest measurement, device info, and runtime configuration."""
    latest = await db.get_latest()
    info = await db.get_device_info()
    return {
        "latest": latest,
        "device": info,
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
    """Historical data points for the requested time range."""
    now = datetime.now()
    if range == "day":
        start = datetime.combine(now.date(), time.min)
        bucket = 0  # raw points
    elif range == "week":
        start = now - timedelta(days=7)
        bucket = 600  # 10-minute buckets
    elif range == "month":
        start = now - timedelta(days=30)
        bucket = 3600  # 1-hour buckets
    else:  # year
        start = now - timedelta(days=365)
        bucket = 86400  # daily buckets

    points = await db.get_range(int(start.timestamp()), int(now.timestamp()), bucket)
    return {"range": range, "bucket_seconds": bucket, "points": points}


@app.get("/api/stats")
async def get_stats():
    """Aggregated statistics with period-over-period comparisons."""
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
    total_kwh = await db.get_total_energy()

    co2_kg = total_kwh * CO2_KG_PER_KWH
    money_saved = total_kwh * PRICE_PER_KWH

    today_points = await db.get_range(int(today_start.timestamp()), int(now.timestamp()), 0)
    peak_w_today = 0.0
    if today_points:
        peak_w_today = max(((p.get("p1") or 0) + (p.get("p2") or 0)) for p in today_points)

    daily_30d = await db.get_daily_totals(30)

    return {
        "today_kwh": round(today_kwh, 3),
        "yesterday_kwh": round(yesterday_kwh, 3),
        "this_week_kwh": round(this_week_kwh, 3),
        "last_week_kwh": round(last_week_kwh, 3),
        "this_month_kwh": round(this_month_kwh, 3),
        "last_month_kwh": round(last_month_kwh, 3),
        "this_year_kwh": round(this_year_kwh, 3),
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