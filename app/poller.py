"""Background poller for APsystems EZ1-M inverter."""
import asyncio
import logging
from datetime import datetime
from APsystemsEZ1 import APsystemsEZ1M

from .database import Database

logger = logging.getLogger(__name__)


class Poller:
    def __init__(self, inverter_ip: str, port: int, interval: int, db: Database):
        self.inverter = APsystemsEZ1M(inverter_ip, port)
        self.interval = interval
        self.db = db
        self._task: asyncio.Task | None = None
        self._stop = False

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
                    logger.info(f"Connected to inverter {info.deviceId} (FW {info.devVer}), max {info.maxPower}W")
                    break
            except Exception as e:
                logger.warning(f"Initial connect attempt {attempt+1} failed: {e}")
                await asyncio.sleep(min(30, 2 ** attempt))

        while not self._stop:
            ts = int(datetime.now().timestamp())
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
                    )
                    logger.debug(f"Polled: p1={data.p1}W p2={data.p2}W")
                else:
                    await self.db.insert_measurement(ts, None, None, None, None, None, None, online=False)
            except Exception as e:
                logger.warning(f"Poll failed: {e}")
                try:
                    await self.db.insert_measurement(ts, None, None, None, None, None, None, online=False)
                except Exception:
                    pass

            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
