"""FastAPI app for EZ1 Monitor."""
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, time
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .database import Database
from .poller import Poller

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ez1-monitor")

# --- Config (from env) ----------------------------------------------------
INVERTER_IP = os.getenv("INVERTER_IP", "192.168.1.100")
INVERTER_PORT = int(os.getenv("INVERTER_PORT", "8050"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
DB_PATH = os.getenv("DB_PATH", "/data/ez1.db")
INSTALL_KWP = float(os.getenv("INSTALL_KWP", "1.0"))  # for vergleichswerte
# --------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"

db = Database(DB_PATH)
poller = Poller(INVERTER_IP, INVERTER_PORT, POLL_INTERVAL, db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting EZ1 Monitor — inverter at {INVERTER_IP}:{INVERTER_PORT}, poll every {POLL_INTERVAL}s")
    await db.init()
    await poller.start()
    yield
    logger.info("Shutting down ...")
    await poller.stop()


app = FastAPI(title="EZ1 Monitor", lifespan=lifespan)


# ---------------------------- API ----------------------------------------

@app.get("/api/live")
async def get_live():
    """Latest measurement + device info."""
    latest = await db.get_latest()
    info = await db.get_device_info()
    return {
        "latest": latest,
        "device": info,
        "config": {
            "inverter_ip": INVERTER_IP,
            "poll_interval": POLL_INTERVAL,
            "install_kwp": INSTALL_KWP,
        },
    }


@app.get("/api/history")
async def get_history(
    range: str = Query("day", pattern="^(day|week|month|year)$"),
):
    """Historical data for the given range."""
    now = datetime.now()
    if range == "day":
        start = datetime.combine(now.date(), time.min)
        bucket = 0  # raw points
    elif range == "week":
        start = now - timedelta(days=7)
        bucket = 600  # 10-min buckets
    elif range == "month":
        start = now - timedelta(days=30)
        bucket = 3600  # 1-hour buckets
    else:  # year
        start = now - timedelta(days=365)
        bucket = 86400  # daily

    points = await db.get_range(int(start.timestamp()), int(now.timestamp()), bucket)
    return {"range": range, "bucket_seconds": bucket, "points": points}


@app.get("/api/stats")
async def get_stats():
    """Summary statistics with comparisons."""
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
        """Sum of daily MAX(e1+e2) values in the date range."""
        daily = await db.get_range(int(start.timestamp()), int(end.timestamp()), 86400)
        # Each bucket is one day with MAX(e1), MAX(e2). e1/e2 are reset at midnight.
        return sum(((d.get("e1") or 0) + (d.get("e2") or 0)) for d in daily)

    # Today / Yesterday
    today_kwh = await energy_between(today_start, now)
    yesterday_kwh = await energy_between(yesterday_start, today_start)

    # This week vs last week (full last week)
    this_week_kwh = await energy_between(this_week_start, now)
    last_week_kwh = await energy_between(last_week_start, this_week_start)

    # This month vs last month
    this_month_kwh = await energy_between(this_month_start, now)
    last_month_kwh = await energy_between(last_month_start, this_month_start)

    # Year + total
    this_year_kwh = await energy_between(this_year_start, now)
    total_kwh = await db.get_total_energy()

    # Vergleichswerte (CO2 + Cents based on ~0.35 €/kWh)
    co2_kg = total_kwh * 0.38  # German grid mix ~380g/kWh
    saved_eur = total_kwh * 0.35

    # Today peak power
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
        "money_saved_eur": round(saved_eur, 2),
        "daily_30d": daily_30d,
    }


# ---------------------------- Static frontend ----------------------------

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
