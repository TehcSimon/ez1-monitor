"""Background poller for APsystems EZ1-M inverter.

Adaptive polling: at the normal interval while the inverter is online,
slowed down by STANDBY_FACTOR while offline (with an absolute cap).
This avoids hammering an offline inverter all night and keeps the DB
from filling up with offline-marker rows.

Each successful measurement is stamped with the carbon-intensity factor
(gCO2eq/kWh) that was active at the time. This lets the lifetime CO2
calculation be historically accurate — daytime measurements get a low
factor (solar-heavy grid), nighttime ones get a high factor (more coal/gas),
all without any further frontend logic.
"""
import asyncio
import logging
from datetime import datetime
from APsystemsEZ1 import APsystemsEZ1M

from .database import Database
from .co2 import CarbonState, resolve_current

logger = logging.getLogger(__name__)

# Multiply normal POLL_INTERVAL by this when offline. Capped by STANDBY_CAP_S.
STANDBY_FACTOR = 10
STANDBY_CAP_S = 300  # 5 minutes


class Poller:
    def __init__(self, inverter_ip: str, port: int, interval: int,
                 db: Database, carbon_state: CarbonState):
        self.inverter = APsystemsEZ1M(inverter_ip, port)
        self.interval = interval
        self.db = db
        self.carbon_state = carbon_state
        self._task: asyncio.Task | None = None
        self._stop = False
        # Have we already written one offline marker since the last successful poll?
        # If yes, we skip writing further offline rows until the inverter comes back.
        self._offline_marker_written = False

    @property
    def standby_interval(self) -> int:
        return min(self.interval * STANDBY_FACTOR, STANDBY_CAP_S)

    async def start(self):
        self._stop = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        # Initial device info fetch (retry a few times until inverter is reachable)
        for attempt in range(10):
            try:
                info = await self.inverter.get_device_info()
                if info:
                    await self.db.update_device_info(
                        device_id=info.deviceId or "",
                        serial_number=info.devVer or "",
                        min_power=info.minPower or 0,
                        max_power=info.maxPower or 800,
                    )
                    logger.info(
                        f"Connected to inverter {info.deviceId} "
                        f"(FW {info.devVer}), max {info.maxPower}W"
                    )
                    break
            except Exception as e:
                logger.warning(f"Initial connect attempt {attempt+1} failed: {e}")
                await asyncio.sleep(min(30, 2 ** attempt))

        while not self._stop:
            ts = int(datetime.now().timestamp())
            sleep_seconds = self.interval
            # Resolve current CO2 factor — uses live value if recent, falls
            # back to last-known/avg/static per the three-tier cascade in
            # co2.resolve_current(). Stamped on each measurement so the
            # lifetime calculation reflects what the grid actually was at
            # the time, not a single static guess.
            co2_g = resolve_current(self.carbon_state).g_per_kwh
            try:
                data = await self.inverter.get_output_data()
                if data:
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
                    )
                    logger.debug(f"Polled: p1={data.p1}W p2={data.p2}W co2={co2_g:.0f}")
                    # Online: reset offline marker, use normal interval
                    self._offline_marker_written = False
                    sleep_seconds = self.interval
                else:
                    sleep_seconds = await self._record_offline(ts)
            except Exception as e:
                logger.debug(f"Poll failed: {e}")
                sleep_seconds = await self._record_offline(ts)

            try:
                await asyncio.sleep(sleep_seconds)
            except asyncio.CancelledError:
                break

    async def _record_offline(self, ts: int) -> int:
        """Record an offline marker (only the first one per offline streak)
        and return the next sleep interval."""
        if not self._offline_marker_written:
            try:
                # Offline marker: no power data, but we still stamp the CO2
                # factor in case the next online poll happens later — keeps
                # the time-series consistent.
                await self.db.insert_measurement(
                    ts, None, None, None, None, None, None,
                    online=False, co2_g_per_kwh=None,
                )
                self._offline_marker_written = True
                logger.info(
                    f"Inverter went offline — slowing polls to "
                    f"{self.standby_interval}s until back online"
                )
            except Exception:
                pass
        return self.standby_interval
