"""Background poller for APsystems EZ1 inverters.

Adaptive polling: at the normal interval while the inverter is online,
slowed down by STANDBY_FACTOR while offline (with an absolute cap).
This avoids hammering an offline inverter all night and keeps the DB
from filling up with offline-marker rows.

A failed poll alone doesn't mean offline: the inverter's local API drops a
request now and then during normal production. Treating that as offline showed
a red error on the dashboard and, with the slowed-down polling, cost minutes of
data. Only OFFLINE_AFTER_FAILED_POLLS failures in a row count; until then the
poller just tries again at the normal interval.

Each successful measurement is stamped with the carbon-intensity factor
(gCO2eq/kWh) that was active at the time. This lets the lifetime CO2
calculation be historically accurate — daytime measurements get a low
factor (solar-heavy grid), nighttime ones get a high factor (more coal/gas),
all without any further frontend logic.
"""
import asyncio
import logging
import math
import time
from datetime import datetime
from APsystemsEZ1 import APsystemsEZ1M

from .database import Database
from .co2 import CarbonState, resolve_current

logger = logging.getLogger(__name__)

# Multiply normal POLL_INTERVAL by this when offline. Capped by STANDBY_CAP_S.
STANDBY_FACTOR = 10
STANDBY_CAP_S = 300  # 5 minutes

# Failed polls in a row before the inverter counts as offline.
OFFLINE_AFTER_FAILED_POLLS = 3

# How often device info and AC limit are re-read. The limit can be changed in
# the APsystems app at any time, the rest only with a firmware update.
DEVICE_INFO_REFRESH_S = 3600

# getOutputDataDetail (temperature, grid voltage and frequency) at most once a
# minute: some EZ1-D firmware needs 20-30 s to answer again after this call,
# which is also why it runs halfway between two regular polls. The second of
# slack keeps a 60 s poll interval from skipping every other cycle.
DETAIL_MIN_SPACING_S = 59
# Pause after no usable answer, or when the regular poll right after this call
# failed: firmware that can't serve it is left alone, with one try per pause.
DETAIL_PAUSE_S = 600
# A reading older than this is no longer stamped on new measurements.
DETAIL_MAX_AGE_S = 180


def _number(value) -> float | None:
    """A numeric field from the detail endpoint. Depending on the firmware
    values arrive as numbers, as strings, as "null" or not at all."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class Poller:
    def __init__(self, inverter_ip: str, port: int, interval: int,
                 db: Database, carbon_state: CarbonState,
                 price_per_kwh: float = 0.0):
        self.inverter = APsystemsEZ1M(inverter_ip, port)
        self.interval = interval
        self.db = db
        self.carbon_state = carbon_state
        # Stamped on every measurement so the lifetime "money saved"
        # calculation stays historically accurate across tariff changes
        # (same pattern as the CO2 factor).
        self.price_per_kwh = price_per_kwh
        self._task: asyncio.Task | None = None
        self._stop = False
        # Failed polls since the last successful one.
        self._failed_polls = 0
        # Have we already written one offline marker since the last successful poll?
        # If yes, we skip writing further offline rows until the inverter comes back.
        self._offline_marker_written = False
        # Has device info (serial, firmware, AC limit) been stored this
        # process lifetime? Retried after every successful data poll until
        # it succeeds once — see _loop.
        self._device_info_ok = False
        # When device info was last read (time.monotonic), for the hourly refresh.
        self._device_info_at = 0.0
        # Last AC limit the inverter reported. Survives a failed getMaxPower,
        # so a dropped request can't flip the limit to the settable maximum.
        self._ac_limit: int | None = None
        # Latest getOutputDataDetail reading, as measurement columns, and the
        # bookkeeping around it (see _sleep_until_next_poll, _poll_detail).
        self._detail: dict | None = None
        self._detail_at = 0.0
        self._detail_next_at = 0.0
        self._detail_just_polled = False
        # When the last poll cycle completed (time.monotonic) — see is_healthy.
        self._last_cycle_at = 0.0

    @property
    def standby_interval(self) -> int:
        return min(self.interval * STANDBY_FACTOR, STANDBY_CAP_S)

    def is_healthy(self) -> bool:
        """Is the poll loop alive and still completing its cycles?

        Deliberately says nothing about the inverter: asleep at night is its
        normal state, and a failed poll is a completed cycle. What this catches
        is the poller itself being stuck, e.g. on a database it can't write to.
        Allows for three of the longest regular waits, so one slow cycle or a
        passing database lock doesn't count.
        """
        if self._task is None or self._task.done():
            return False
        limit = 3 * max(self.interval, self.standby_interval)
        return time.monotonic() - self._last_cycle_at < limit

    async def start(self):
        self._stop = False
        self._last_cycle_at = time.monotonic()
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _try_update_device_info(self) -> bool:
        """Fetch device info from the inverter and store it. Returns True on
        success. Failures are logged at DEBUG only — the caller retries on
        the next successful data poll anyway."""
        self._device_info_at = time.monotonic()
        try:
            info = await self.inverter.get_device_info()
            if not info:
                return False
            limit = await self._read_ac_limit() or info.maxPower or 800
            await self.db.update_device_info(
                device_id=info.deviceId or "",
                firmware=info.devVer or "",
                min_power=info.minPower or 0,
                max_power=limit,
            )
        except Exception as e:
            logger.debug(f"Device info update failed: {e}")
            return False
        if not self._device_info_ok:
            logger.info(
                f"Connected to inverter {info.deviceId} "
                f"(FW {info.devVer}), AC limit {limit}W"
            )
        return True

    async def _read_ac_limit(self) -> int | None:
        """The AC limit in force. deviceInfo.maxPower is only the highest
        value that can be set: an 1800 W EZ1-D limited to 800 W still reports
        1800 there. Returns the last known limit if the request fails, or None
        if there has never been one (firmware without this endpoint)."""
        try:
            limit = await self.inverter.get_max_power()
        except Exception as e:
            logger.debug(f"AC limit read failed: {e}")
            limit = None
        if limit:
            self._ac_limit = limit
        return self._ac_limit

    async def _loop(self):
        # Device info: try once at start, then keep retrying after every
        # successful data poll until it lands. The previous fixed 10-attempt
        # startup loop gave up after ~3 minutes, so a container (re)started
        # at night — inverter sleeping — showed placeholder device data
        # (S/N, firmware, and the 800 W AC-limit fallback that also scales
        # the fixed day-chart axis) until the next restart.
        self._device_info_ok = await self._try_update_device_info()

        while not self._stop:
            try:
                sleep_seconds = await self._poll_once()
                self._last_cycle_at = time.monotonic()
            except Exception:
                # Not the inverter, _poll_once handles its failures. This is
                # the database or our own code, and it must not end the loop:
                # nothing would ever restart it. If it persists, the missing
                # heartbeat shows up in /health.
                logger.exception("Poll cycle failed")
                sleep_seconds = self.interval

            try:
                await self._sleep_until_next_poll(sleep_seconds)
            except asyncio.CancelledError:
                break

    async def _sleep_until_next_poll(self, seconds: float) -> None:
        """Wait for the next poll, reading the detail values halfway there
        when they are due and the inverter is up. Part of the one poll loop,
        so this request can never overlap a regular poll."""
        if self._failed_polls or time.monotonic() < self._detail_next_at:
            await asyncio.sleep(seconds)
            return
        await asyncio.sleep(seconds / 2)
        started = time.monotonic()
        await self._poll_detail()
        await asyncio.sleep(max(0.0, seconds / 2 - (time.monotonic() - started)))

    async def _poll_detail(self) -> None:
        """Read temperature, grid voltage and grid frequency for the next
        measurements. Never raises: these values are a bonus, and the regular
        poll must carry on without them.

        Goes through the library's request helper (pinned version) as it has
        no method for this endpoint. Without retries: a firmware that doesn't
        serve it shouldn't be asked four times in a row.
        """
        started = time.monotonic()
        self._detail_just_polled = True
        try:
            response = await self.inverter._request("getOutputDataDetail", retry=0)
            data = (response or {}).get("data") or {}
        except Exception as e:
            logger.debug(f"Detail poll failed: {e}")
            data = {}
        detail = {
            "temp_c": _number(data.get("t")),
            "grid_v": _number(data.get("gv")),
            "grid_hz": _number(data.get("gf")),
        }
        if all(value is None for value in detail.values()):
            self._detail = None
            self._detail_next_at = started + DETAIL_PAUSE_S
            return
        self._detail = detail
        self._detail_at = started
        self._detail_next_at = started + DETAIL_MIN_SPACING_S

    def _fresh_detail(self) -> dict:
        """The detail reading to stamp on a measurement, if still current."""
        if self._detail and time.monotonic() - self._detail_at < DETAIL_MAX_AGE_S:
            return self._detail
        return {}

    async def _poll_once(self) -> int:
        """Poll once and return the seconds to wait until the next poll.

        Only a failed request counts against the inverter. A failing database
        write raises instead, so it can't be mistaken for an offline inverter.
        """
        ts = int(datetime.now().timestamp())
        detail_just_polled, self._detail_just_polled = self._detail_just_polled, False
        try:
            data = await self.inverter.get_output_data()
        except Exception as e:
            logger.debug(f"Poll failed: {e}")
            data = None
        if not data:
            if detail_just_polled:
                # Possibly our doing, see DETAIL_MIN_SPACING_S.
                self._detail_next_at = time.monotonic() + DETAIL_PAUSE_S
            return await self._record_failed_poll(ts)

        # Online: reset the failure streak and the offline marker
        self._failed_polls = 0
        self._offline_marker_written = False
        # Resolve current CO2 factor — uses live value if recent, falls
        # back to last-known/avg/static per the three-tier cascade in
        # co2.resolve_current(). Stamped on each measurement so the
        # lifetime calculation reflects what the grid actually was at
        # the time, not a single static guess.
        co2_g = resolve_current(self.carbon_state).g_per_kwh
        await self.db.insert_measurement(
            timestamp=ts,
            p1=data.p1,
            p2=data.p2,
            e1=data.e1,
            e2=data.e2,
            te1=data.te1,
            te2=data.te2,
            online=True,
            co2_g_per_kwh=co2_g,
            price_per_kwh=self.price_per_kwh,
            **self._fresh_detail(),
        )
        logger.debug(f"Polled: p1={data.p1}W p2={data.p2}W co2={co2_g:.0f}")
        if (not self._device_info_ok
                or time.monotonic() - self._device_info_at >= DEVICE_INFO_REFRESH_S):
            # Inverter is clearly reachable now — fill in the device info we
            # couldn't get at startup, or pick up a changed AC limit.
            updated = await self._try_update_device_info()
            self._device_info_ok = self._device_info_ok or updated
        return self.interval

    async def _record_failed_poll(self, ts: int) -> int:
        """Count a failed poll and return the next sleep interval. Once the
        inverter counts as offline, record that with a marker (only the first
        one per offline streak) and slow down."""
        self._failed_polls += 1
        if self._failed_polls < OFFLINE_AFTER_FAILED_POLLS:
            return self.interval
        if not self._offline_marker_written:
            # Offline marker: no power data, and we leave co2_g_per_kwh
            # and price_per_kwh unset because there's no production to
            # attribute a factor to. The energy-weighted averages skip
            # NULL rows anyway, so this keeps the lifetime calculations
            # honest.
            await self.db.insert_measurement(
                ts, None, None, None, None, None, None,
                online=False, co2_g_per_kwh=None, price_per_kwh=None,
            )
            self._offline_marker_written = True
            logger.info(
                f"Inverter went offline — slowing polls to "
                f"{self.standby_interval}s until back online"
            )
        return self.standby_interval
