import asyncio
from contextlib import suppress
from typing import Any, Optional, Dict, Iterable
import logging
import json
from enum import Enum

from gmqtt import Client
import uuid
import stabilizer
import numpy as np

logger = logging.getLogger(__name__)

Y_MAX = stabilizer.voltage_to_machine_units(stabilizer.DAC_FULL_SCALE)


def _int_to_bytes(i):
    return i.to_bytes(i.bit_length() // 8 + 1, byteorder="little")


def starts_with(string, prefix) -> bool:
    return len(string) >= len(prefix) and string[:len(prefix)] == prefix


class MqttInterface:
    """
    Wraps a gmqtt Client to provide a request/response-type interface using the MQTT 5
    response topics/correlation data machinery.

    A timeout is also applied to every request (which is necessary for robustness, as
    Stabilizer only supports QoS 0 for now).
    """

    def __init__(self,
                 client: Client,
                 topic_base: str,
                 timeout: float,
                 maxsize: int = 512):
        self._client = client
        self._topic_base = topic_base
        self._pending = {}
        self._timeout = timeout
        self._maxsize = maxsize

        #: Use incrementing sequence id as correlation data to map responses to requests
        #: (together with client id).
        self._next_seq_id = 0

        # Generate a random client ID (no real reason to use UUID here over another
        # source of randomness).
        client_id = str(uuid.uuid4()).split("-")[0]
        self._response_base = f"{topic_base}/response_{client_id}"
        self._client.subscribe(f"{self._response_base}/#")

        self._client.on_message = self._on_message

    async def request(self, topic: str, argument: Any, retain: bool = False):
        if len(self._pending) > self._maxsize:
            # By construction, `correlation_data` should always be removed from
            # `_pending` either by `_on_message()` or after `_timeout`. If something
            # goes wrong, however the dictionary could grow indefinitely.
            raise RuntimeError("Too many unhandled requests")
        result = asyncio.Future()
        correlation_data = _int_to_bytes(self._next_seq_id)

        self._pending[correlation_data] = result

        payload = json.dumps(argument).encode("utf-8")
        self._client.publish(
            f"{self._topic_base}/{topic}",
            payload,
            qos=0,
            retain=retain,
            response_topic=f"{self._response_base}/{topic}",
            correlation_data=correlation_data,
        )
        self._next_seq_id += 1

        async def fail_after_timeout():
            await asyncio.sleep(self._timeout)
            result.set_exception(
                TimeoutError(f"No response to {topic} request after {self._timeout} s"))
            self._pending.pop(correlation_data)

        _, pending = await asyncio.wait(
            [result, asyncio.create_task(fail_after_timeout())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
            with suppress(asyncio.CancelledError):
                await p
        return await result

    def _on_message(self, _client, topic, payload, _qos, properties) -> int:
        if not starts_with(topic, self._response_base):
            logger.debug("Ignoring unrelated topic: %s", topic)
            return 0

        cd = properties.get("correlation_data", [])
        if len(cd) != 1:
            logger.warning(
                ("Received response without (valid) correlation data"
                 "(topic '%s', payload %s) "),
                topic,
                payload,
            )
            return 0
        seq_id = cd[0]

        if seq_id not in self._pending:
            # This is fine if Stabilizer restarts, though.
            logger.warning("Received unexpected/late response for '%s' (id %s)", topic,
                           seq_id)
            return 0

        result = self._pending.pop(seq_id)
        if not result.cancelled():
            try:
                # Would like to json.loads() here, but the miniconf responses are
                # unfortunately plain strings still (see quartiq/miniconf#32).
                result.set_result(payload)
            except BaseException as e:
                err = ValueError(f"Failed to parse response for '{topic}'")
                err.__cause__ = e
                result.set_exception(err)
        return 0


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
        y_offset: float = 0.0,
        y_min: float = -Y_MAX,
        y_max: float = Y_MAX,
    ):
        key = f"{self.iir_ch_topic_base}/{channel}/{iir_idx}"
        value = {
            "ba": list(ba),
            "u": stabilizer.voltage_to_machine_units(y_offset),
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
