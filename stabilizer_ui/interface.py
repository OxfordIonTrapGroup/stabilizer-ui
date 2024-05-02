import stabilizer
import asyncio
import numpy as np
import logging
import json

from typing import Any, Iterable, Optional
from sipyco import pc_rpc

from .mqtt import MqttInterface

logger = logging.getLogger(__name__)

Y_MAX = stabilizer.voltage_to_machine_units(stabilizer.DAC_FULL_SCALE)


def starts_with(string, prefix) -> bool:
    return len(string) >= len(prefix) and string[:len(prefix)] == prefix


class AbstractStabilizerInterface:
    """
    Shim for controlling stabilizer over MQTT
    """

    def __init__(self):
        self._interface_set = asyncio.Event()
        self._interface: Optional[MqttInterface] = None

    def set_interface(self, interface: MqttInterface) -> None:
        self._interface = interface
        self._interface_set.set()

    async def change(self, *args, **kwargs):
        await self._interface_set.wait()
        await self.triage_setting_change(*args, **kwargs)

    async def triage_setting_change(self):
        raise NotImplementedError

    async def set_pi_gains(self, channel: int, iir_idx: int, p_gain: float,
                           i_gain: float):
        b0 = i_gain * 2 * np.pi * stabilizer.SAMPLE_PERIOD + p_gain
        b1 = -p_gain
        await self.set_iir(channel, iir_idx, [b0, b1, 0, 1, 0])

    async def set_iir(
        self,
        channel: int,
        iir_idx: int,
        ba: Iterable,
        x_offset: float = 0.0,
        y_offset: float = 0.0,
        y_min: float = -Y_MAX,
        y_max: float = Y_MAX,
    ):
        forward_gain = sum(ba[:3])
        if forward_gain == 0 and x_offset != 0:
            logger.warning("Filter has no DC gain but x_offset is non-zero")
        key = f"{self.iir_ch_topic_base}/{channel}/{iir_idx}"
        value = {
            "ba": list(ba),
            "u": stabilizer.voltage_to_machine_units(y_offset + forward_gain * x_offset),
            "min": stabilizer.voltage_to_machine_units(y_min),
            "max": stabilizer.voltage_to_machine_units(y_max),
        }
        await self.request_settings_change(key, value)

    def publish_ui_change(self, topic: str, argument: Any):
        payload = json.dumps(argument).encode("utf-8")
        self._interface._client.publish(f"{self._interface._topic_base}/{topic}",
                                        payload,
                                        qos=0,
                                        retain=True)

    async def request_settings_change(self, key: str, value: Any):
        """
        Write to the miniconf-provided topics, which currently returns a
        string message as a reply; should really be JSON/… instead, see
        quartiq/miniconf#32.
        """
        msg = await self._interface.request(key, value, retain=True)
        if starts_with(msg, "Settings fail"):
            logger.warning("Stabilizer reported failure to write setting: '%s'", msg)


class WavemeterInterface:
    """Wraps a connection to the WAnD wavemeter server, offering an interface to query
    a single channel while automatically reconnecting on failure/timeout.
    """

    def __init__(self, host: str, port: int, channel: str, timeout: float):
        self._client = None
        self._host = host
        self._port = port
        self._channel = channel
        self._timeout = timeout

    async def try_connect(self) -> None:
        try:
            self._client = pc_rpc.AsyncioClient()
            await asyncio.wait_for(self._client.connect_rpc(self._host, self._port,
                                                            "control"),
                                   timeout=self._timeout)
        except Exception:
            logger.exception("Failed to connect to WAnD server")
            self._client = None

    def is_connected(self) -> bool:
        return self._client is not None

    async def get_freq_offset(self, age=0) -> float:
        while True:
            while not self.is_connected():
                logger.info("Reconnecting to WAnD server")
                await self.try_connect()

            try:
                return await asyncio.wait_for(self._client.get_freq(laser=self._channel,
                                                                    age=age,
                                                                    priority=10,
                                                                    offset_mode=True),
                                              timeout=self._timeout)
            except Exception:
                logger.exception(f"Error getting {self._channel} wavemeter reading")
                # Drop connection (to later reconnect). In regular operation, about the
                # only reason this should happen is due to timeouts after server
                # restarts/network weirdness, so don't bother distinguishing between
                # error types.
                self._client.close_rpc()
                self._client = None