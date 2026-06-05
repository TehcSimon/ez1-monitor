"""Carbon-intensity data source and three-tier resolver.

This module handles the optional Electricity Maps integration. When a token
is configured, the container polls the API hourly and stores the result in
an in-memory cache. The resolver function returns the most appropriate
value depending on data freshness:

    0-6h   since last successful poll  →  live     (display "Live")
    6-48h  since last successful poll  →  stale    (display "Letzter Wert · vor Nh")
    >48h   since last successful poll  →  rolling-average from history
    no token / no data ever            →  static fallback from env var

The Electricity Maps API endpoint we use is the Home-Assistant-flavored one
at https://api.electricitymap.org/v3/home-assistant. The zone is bound to
the API token on the server side (chosen in the Electricity Maps portal),
so the request takes no zone parameter.

Response shape (from a real curl test against the live endpoint):

    {
      "_disclaimer": "This data is the exclusive property of Electricity Maps...",
      "status": "ok",
      "countryCode": "DE",
      "data": {
        "datetime": "2026-06-05T22:00:00.000Z",
        "carbonIntensity": 386,
        "fossilFuelPercentage": 43.06
      },
      "units": { "carbonIntensity": "gCO2eq/kWh" }
    }
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# The free Home-Assistant tier endpoint. Zone is determined server-side from
# the token, NOT passed as a parameter — passing ?zone=... returns a "Zone
# does not exist" error on this endpoint.
HA_ENDPOINT = "https://api.electricitymap.org/v3/home-assistant"

# Poll once per hour. Carbon intensity updates hourly, and the free tier
# allows 50 req/hour, so we have 50× headroom for retries / restarts.
POLL_INTERVAL_S = 3600

# Three-tier cascade thresholds (in seconds since last successful poll).
FRESH_AGE_S  = 6 * 3600       # 0-6h:  "Live"
STALE_AGE_S  = 48 * 3600      # 6-48h: "Letzter Wert · vor Nh"
                              # >48h:  rolling average

REQUEST_TIMEOUT_S = 10


@dataclass
class CarbonState:
    """Mutable runtime state for the CO2 module. One instance per process.

    Lives in memory only — we don't persist this state across restarts
    because (a) the rolling average can be re-derived from the DB on
    startup, and (b) a fresh poll will always run within an hour of
    container start anyway.
    """
    # Configuration
    token: str = ""               # Electricity Maps API token (empty = disabled)
    static_g_per_kwh: float = 380.0   # Env fallback (CO2_KG_PER_KWH × 1000)

    # Latest successful poll
    last_g_per_kwh: Optional[float] = None
    last_datetime: Optional[str] = None       # ISO 8601 from API
    last_fossil_pct: Optional[float] = None
    last_country_code: Optional[str] = None
    last_success_at: Optional[datetime] = None    # UTC, when poll succeeded

    # Rolling average across all successful polls (since process start)
    rolling_sum: float = 0.0
    rolling_count: int = 0

    # Diagnostics
    consecutive_failures: int = 0
    last_error: Optional[str] = None


@dataclass
class CarbonResolution:
    """The resolved CO2 factor + provenance label for the UI."""
    g_per_kwh: float
    # Provenance: one of "live", "stale", "avg", "static"
    source: str
    # Bonus fields, may be None depending on source
    datetime: Optional[str] = None        # ISO 8601 of when the value was measured
    fossil_pct: Optional[float] = None
    country_code: Optional[str] = None
    age_seconds: Optional[int] = None     # how old is the value (None for static)


async def fetch_carbon_intensity(token: str) -> Optional[dict]:
    """Single HTTP call to Electricity Maps. Returns parsed payload or None."""
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            r = await client.get(
                HA_ENDPOINT,
                headers={"auth-token": token},
            )
            if r.status_code != 200:
                logger.warning(
                    f"Electricity Maps: HTTP {r.status_code} — {r.text[:200]}"
                )
                return None
            data = r.json()
    except httpx.TimeoutException:
        logger.warning("Electricity Maps: request timed out")
        return None
    except Exception as e:
        logger.warning(f"Electricity Maps: fetch failed: {e}")
        return None

    if data.get("status") != "ok":
        logger.warning(f"Electricity Maps: non-ok status: {data.get('status')}")
        return None

    payload = data.get("data") or {}
    g_per_kwh = payload.get("carbonIntensity")
    if g_per_kwh is None:
        logger.warning("Electricity Maps: response missing carbonIntensity")
        return None

    return {
        "g_per_kwh": float(g_per_kwh),
        "datetime": payload.get("datetime"),
        "fossil_pct": payload.get("fossilFuelPercentage"),
        "country_code": data.get("countryCode"),
    }


async def poll_once(state: CarbonState) -> bool:
    """One polling tick. Updates state in place. Returns True on success."""
    if not state.token:
        return False

    result = await fetch_carbon_intensity(state.token)
    if not result:
        state.consecutive_failures += 1
        state.last_error = "API call failed"
        return False

    state.last_g_per_kwh = result["g_per_kwh"]
    state.last_datetime = result["datetime"]
    state.last_fossil_pct = result["fossil_pct"]
    state.last_country_code = result["country_code"]
    state.last_success_at = datetime.now(timezone.utc)

    state.rolling_sum += result["g_per_kwh"]
    state.rolling_count += 1

    state.consecutive_failures = 0
    state.last_error = None
    logger.info(
        f"Electricity Maps: {result['g_per_kwh']:.0f} gCO2eq/kWh "
        f"(zone {result.get('country_code')}, "
        f"fossil {result.get('fossil_pct')}%, "
        f"avg over {state.rolling_count} samples: "
        f"{state.rolling_sum / state.rolling_count:.0f})"
    )
    return True


async def poll_loop(state: CarbonState, interval_s: int = POLL_INTERVAL_S):
    """Background task: poll the API on a fixed interval. Runs forever."""
    if not state.token:
        logger.info(
            "Electricity Maps: no token configured — using static fallback "
            f"({state.static_g_per_kwh:.0f} gCO2eq/kWh)"
        )
        return

    # First poll immediately on start so the UI has live data right away
    await poll_once(state)

    while True:
        try:
            await asyncio.sleep(interval_s)
            await poll_once(state)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Electricity Maps poll loop error: {e}")
            # Sleep a bit before retrying to avoid hot-looping on bugs
            await asyncio.sleep(60)


def resolve_current(state: CarbonState) -> CarbonResolution:
    """The three-tier resolver. Called on every /api/live to get the value
    used in CO2-saved calculations and shown in the UI."""
    static_val = state.static_g_per_kwh

    # No token configured — always static
    if not state.token:
        return CarbonResolution(g_per_kwh=static_val, source="static")

    # Token configured but no poll has succeeded yet (e.g. brand-new
    # container, API down since first start)
    if state.last_success_at is None or state.last_g_per_kwh is None:
        return CarbonResolution(g_per_kwh=static_val, source="static")

    age_s = (datetime.now(timezone.utc) - state.last_success_at).total_seconds()

    # 0-6h: live
    if age_s < FRESH_AGE_S:
        return CarbonResolution(
            g_per_kwh=state.last_g_per_kwh,
            source="live",
            datetime=state.last_datetime,
            fossil_pct=state.last_fossil_pct,
            country_code=state.last_country_code,
            age_seconds=int(age_s),
        )

    # 6-48h: stale (last known value, but flagged)
    if age_s < STALE_AGE_S:
        return CarbonResolution(
            g_per_kwh=state.last_g_per_kwh,
            source="stale",
            datetime=state.last_datetime,
            fossil_pct=state.last_fossil_pct,
            country_code=state.last_country_code,
            age_seconds=int(age_s),
        )

    # >48h: rolling average if we have any history
    if state.rolling_count > 0:
        avg = state.rolling_sum / state.rolling_count
        return CarbonResolution(
            g_per_kwh=avg,
            source="avg",
            country_code=state.last_country_code,
            age_seconds=int(age_s),
        )

    # Edge case: token set, was once successful, but rolling state was lost
    return CarbonResolution(g_per_kwh=static_val, source="static")
