"""Background poller for APsystems EZ1-M inverter.

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
        try:
            info = await self.inverter.get_device_info()
            if not info:
                return False
            await self.db.update_device_info(
                device_id=info.deviceId or "",
                firmware=info.devVer or "",
                min_power=info.minPower or 0,
                max_power=info.maxPower or 800,
            )
        except Exception as e:
            logger.debug(f"Device info update failed: {e}")
            return False
        logger.info(
            f"Connected to inverter {info.deviceId} "
            f"(FW {info.devVer}), max {info.maxPower}W"
        )
        return True

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
                await asyncio.sleep(sleep_seconds)
            except asyncio.CancelledError:
                break

    async def _poll_once(self) -> int:
        """Poll once and return the seconds to wait until the next poll.

        Only a failed request counts against the inverter. A failing database
        write raises instead, so it can't be mistaken for an offline inverter.
        """
        ts = int(datetime.now().timestamp())
        try:
            data = await self.inverter.get_output_data()
        except Exception as e:
            logger.debug(f"Poll failed: {e}")
            data = None
        if not data:
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
        )
        logger.debug(f"Polled: p1={data.p1}W p2={data.p2}W co2={co2_g:.0f}")
        if not self._device_info_ok:
            # Inverter is clearly reachable now — fill in the
            # device info we couldn't get at startup.
            self._device_info_ok = await self._try_update_device_info()
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
