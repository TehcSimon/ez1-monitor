"""Optional weather forecast from Open-Meteo, for the sun path card.

Off unless WEATHER_ENABLED is set, and it needs the location from SITE_ADDRESS.
Like Electricity Maps in co2.py, the server polls hourly and the browser never
talks to the service. Open-Meteo needs no API key. Its data is licensed under
CC BY 4.0, so the card has to credit it.

Only coordinates leave the container, rounded to 2 decimals (about 1 km): the
weather models' grid cells are several kilometres wide, so anything finer
would add nothing but a more exact position. They are not logged either.

The answer also holds the past days, because yield_curve.py learns from them
how much of the forecast radiation reaches this plant. It is kept next to the
database, so restarting the container costs no extra request.

    hourly.time                 epoch seconds, the END of each hour
    hourly.shortwave_radiation  W/m², mean over the hour before that time
    hourly.weather_code         WMO code, also drawn hour by hour into the scene
    hourly.temperature_2m       °C
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from .site_map import SiteState, USER_AGENT
from .yield_curve import WINDOW_DAYS

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_CACHE_FILE = "weather.json"
CACHE_VERSION = 1

POLL_INTERVAL_S = 3600
# Same backoff as co2.py: 5 min, then 10 / 20 / 40, capped at the interval.
POLL_RETRY_BASE_S = 300
WAIT_FOR_SITE_S = 30
REQUEST_TIMEOUT_S = 20
# After this long without an answer the estimates fall back to the history.
STALE_AFTER_S = 12 * 3600

COORDINATE_DECIMALS = 2
# The service counts in UTC days, which a local day doesn't line up with. So
# one more than the learning window, and one more than today and tomorrow plus
# the next day: late in a UTC day, tomorrow's last hour is already on the third.
PAST_DAYS = WINDOW_DAYS + 1
FORECAST_DAYS = 4


@dataclass
class WeatherState:
    """Runtime state, one instance per process."""
    enabled: bool = False
    cache_path: Optional[Path] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    fetched_at: Optional[float] = None
    times: list = field(default_factory=list)
    radiation: list = field(default_factory=list)
    codes: list = field(default_factory=list)
    temperatures: list = field(default_factory=list)


def parse_forecast(payload) -> Optional[dict]:
    """The hourly columns of an answer, or None if it isn't usable."""
    try:
        hourly = payload["hourly"]
        columns = {
            "times": hourly["time"],
            "radiation": hourly["shortwave_radiation"],
            "codes": hourly["weather_code"],
            "temperatures": hourly["temperature_2m"],
        }
    except (KeyError, TypeError):
        return None
    if not columns["times"] or len({len(c) for c in columns.values()}) != 1:
        return None
    return columns


def _fresh(state: WeatherState, now: float) -> bool:
    return state.fetched_at is not None and now - state.fetched_at <= STALE_AFTER_S


def radiation_by_hour(state: WeatherState, now: float) -> Optional[dict]:
    """{end of hour: W/m²}, or None while there is no recent forecast."""
    if not _fresh(state, now):
        return None
    return {t: v for t, v in zip(state.times, state.radiation) if v is not None}


def current_conditions(state: WeatherState, now: float) -> Optional[dict]:
    """The hour in progress: {"code": WMO code, "temp_c": °C}, or None."""
    if not _fresh(state, now):
        return None
    hour_end = math.ceil(now / 3600) * 3600
    try:
        i = state.times.index(hour_end)
    except ValueError:
        return None
    if state.codes[i] is None or state.temperatures[i] is None:
        return None
    return {"code": int(state.codes[i]), "temp_c": round(float(state.temperatures[i]), 1)}


def codes_between(state: WeatherState, start: float, end: float, now: float) -> Optional[list]:
    """[[end of hour, WMO code], …] for the hours that overlap start – end,
    or None without a recent forecast or any code in that span."""
    if not _fresh(state, now):
        return None
    hours = [
        [t, int(c)] for t, c in zip(state.times, state.codes)
        if c is not None and start < t < end + 3600
    ]
    return hours or None


def temperatures_between(state: WeatherState, start: float, end: float, now: float) -> Optional[list]:
    """[[time, °C], …] from the last hour before start to the first after end,
    so a line through them covers the whole span; None without a recent
    forecast or any value there. Unlike the radiation, the temperature holds
    for the moment it is stamped with."""
    if not _fresh(state, now):
        return None
    points = [
        [t, round(float(v), 1)] for t, v in zip(state.times, state.temperatures)
        if v is not None and start - 3600 < t < end + 3600
    ]
    return points or None


def load_cache(state: WeatherState) -> None:
    """Restore the last answer, if it was fetched for the same place."""
    if state.cache_path is None:
        return
    try:
        cached = json.loads(state.cache_path.read_text(encoding="utf-8"))
        if (cached["version"], cached["lat"], cached["lon"]) != (CACHE_VERSION, state.lat, state.lon):
            return
        columns = {k: cached[k] for k in ("times", "radiation", "codes", "temperatures")}
        fetched_at = float(cached["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    if len({len(c) for c in columns.values()}) != 1:
        return
    state.fetched_at = fetched_at
    state.times, state.radiation = columns["times"], columns["radiation"]
    state.codes, state.temperatures = columns["codes"], columns["temperatures"]


def save_cache(state: WeatherState) -> None:
    if state.cache_path is None:
        return
    payload = {
        "version": CACHE_VERSION, "lat": state.lat, "lon": state.lon,
        "fetched_at": state.fetched_at, "times": state.times, "radiation": state.radiation,
        "codes": state.codes, "temperatures": state.temperatures,
    }
    temporary = state.cache_path.with_name(state.cache_path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, state.cache_path)
    except OSError as e:
        logger.warning(f"Weather: could not write the cache ({type(e).__name__})")


async def poll_once(state: WeatherState, client: httpx.AsyncClient) -> bool:
    """One request. Updates the state in place. Returns True on success."""
    params = {
        "latitude": state.lat, "longitude": state.lon, "timeformat": "unixtime",
        "hourly": "shortwave_radiation,weather_code,temperature_2m",
        "past_days": PAST_DAYS, "forecast_days": FORECAST_DAYS,
    }
    try:
        response = await client.get(FORECAST_URL, params=params, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as e:
        logger.warning(f"Weather: request failed ({type(e).__name__})")
        return False
    if response.status_code != 200:
        logger.warning(f"Weather: Open-Meteo answered HTTP {response.status_code}")
        return False
    try:
        columns = parse_forecast(response.json())
    except ValueError:
        columns = None
    if columns is None:
        logger.warning("Weather: Open-Meteo sent an unreadable answer")
        return False
    state.times, state.radiation = columns["times"], columns["radiation"]
    state.codes, state.temperatures = columns["codes"], columns["temperatures"]
    state.fetched_at = time.time()
    save_cache(state)
    return True


async def poll_loop(state: WeatherState, site: SiteState, interval_s: int = POLL_INTERVAL_S) -> None:
    """Background task: poll on a fixed interval. Runs forever."""
    if not state.enabled:
        logger.info("Weather: off (WEATHER_ENABLED not set)")
        return
    if not site.address:
        logger.warning("Weather: WEATHER_ENABLED needs SITE_ADDRESS for the location, staying off")
        return
    while site.lat is None:
        await asyncio.sleep(WAIT_FOR_SITE_S)
    state.lat, state.lon = round(site.lat, COORDINATE_DECIMALS), round(site.lon, COORDINATE_DECIMALS)
    load_cache(state)

    # A restart within the hour reuses the last answer.
    delay = max(0.0, state.fetched_at + interval_s - time.time()) if state.fetched_at else 0.0
    failures = 0
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        while True:
            try:
                await asyncio.sleep(delay)
                if await poll_once(state, client):
                    if failures or delay == 0.0:
                        logger.info("Weather: forecast received")
                    failures = 0
                    delay = interval_s
                else:
                    failures += 1
                    delay = min(interval_s, POLL_RETRY_BASE_S * 2 ** min(failures - 1, 4))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Weather: poll loop error ({type(e).__name__})")
                delay = 60
