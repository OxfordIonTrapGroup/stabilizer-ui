import asyncio
from contextlib import suppress
from gmqtt import Client
from typing import Any, Optional, Dict, Iterable
import logging
import json
import uuid
from enum import Enum, unique
import stabilizer
import numpy as np

logger = logging.getLogger(__name__)


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
    def __init__(self, client: Client, topic_base: str, timeout: float, maxsize: int = 512):
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
            # By construction, `correlation_data` should always be removed from `_pending`
            # either by `_on_message()` or after `_timeout`. If something goes wrong, however
            # the dictionary could grow indefinitely.
            raise RuntimeError("Too many unhandled requests")
        result = asyncio.Future()
        correlation_data = _int_to_bytes(self._next_seq_id)

        self._pending[correlation_data] = result

        payload = json.dumps(argument).encode("utf-8")
        self._client.publish(f"{self._topic_base}/{topic}",
                             payload,
                             qos=0,
                             retain=retain,
                             response_topic=f"{self._response_base}/{topic}",
                             correlation_data=correlation_data)
        self._next_seq_id += 1

        async def fail_after_timeout():
            await asyncio.sleep(self._timeout)
            result.set_exception(
                TimeoutError(f"No response to {topic} request after {self._timeout} s"))
            self._pending.pop(correlation_data)

        _, pending = await asyncio.wait(
            [result, asyncio.create_task(fail_after_timeout())],
            return_when=asyncio.FIRST_COMPLETED)
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
                "Received response without (valid) correlation data (topic '%s', payload %s)",
                topic, payload)
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


@unique
class Settings(Enum):
    fast_p_gain = "ui/fast_gains/proportional"
    fast_i_gain = "ui/fast_gains/integral"
    fast_notch_enable = "ui/fast_notch_enable"
    fast_notch_frequency = "ui/fast_notch/frequency"
    fast_notch_quality_factor = "ui/fast_notch/quality_factor"
    slow_p_gain = "ui/slow_gains/proportional"
    slow_i_gain = "ui/slow_gains/integral"
    slow_enable = "ui/slow_enable"
    lock_mode = "ui/lock_mode"
    gain_ramp_time = "settings/gain_ramp_time"
    ld_threshold = "settings/ld_threshold"
    ld_reset_time = "settings/ld_reset_time"
    adc1_routing = "settings/adc1_routing"
    aux_ttl_out = "settings/aux_ttl_out"
    stream_target = "settings/stream_target"


class StabilizerInterface:
    """
    Shim for controlling `l674` stabilizer over MQTT; for the relock task to have something
    to hold on to before the Stabilizer write task has finished initial bringup of the
    MQTT connection. (Could potentially just use two MQTT connections instead, although
    that seems a bit wasteful.)
    """
    #: Settings which are directly addressed to Stabilizer.
    native_settings = {
        Settings.gain_ramp_time,
        Settings.ld_threshold,
        Settings.ld_reset_time,
        Settings.adc1_routing,
        Settings.aux_ttl_out,
        Settings.stream_target,
    }
    #: IIR[0][0]
    fast_pid_settings = {
        Settings.fast_p_gain,
        Settings.fast_i_gain,
    }
    #: IIR[0][1]
    fast_notch_settings = {
        Settings.fast_notch_enable,
        Settings.fast_notch_frequency,
        Settings.fast_notch_quality_factor,
    }
    #: IIR[1][0]
    slow_pid_settings = {
        Settings.slow_p_gain,
        Settings.slow_i_gain,
        Settings.slow_enable,
    }
    read_adc1_filtered_topic = "read_adc1_filtered"
    iir_ch_topic_base = "settings/iir_ch"

    def __init__(self):
        self._interface_set = asyncio.Event()
        self._interface: Optional[MqttInterface] = None

    def set_interface(self, interface: MqttInterface) -> None:
        self._interface = interface
        self._interface_set.set()

    async def read_adc(self) -> float:
        await self._interface_set.wait()
        # Argument irrelevant.
        return float(await self._interface.request(self.read_adc1_filtered_topic, 0))

    async def change(self, setting: Settings, all_values: Dict[Settings, Any]):
        await self._interface_set.wait()

        if setting in self.native_settings:
            await self._request_settings_change(setting.value, all_values[setting])
        else:
            self._publish_ui_change(setting.value, all_values[setting])

        if (setting is Settings.lock_mode
                or setting in self.fast_pid_settings
                or setting in self.slow_pid_settings):
            lock_mode = all_values[Settings.lock_mode]
            if lock_mode != "Enabled" and setting is not Settings.lock_mode:
                # Ignore gain changes if lock is not enabled.
                pass
            elif lock_mode == "Disabled":
                await self._set_pi_gains(channel=0, p_gain=0.0, i_gain=0.0)
                await self._set_pi_gains(channel=1, p_gain=0.0, i_gain=0.0)
            elif lock_mode == "RampPassThrough":
                # Gain 5 gives approximately ±10 V when driven using the Vescent servo box ramp.
                await self._set_pi_gains(channel=0, p_gain=0.5, i_gain=0.0)
                await self._set_pi_gains(channel=1, p_gain=0.0, i_gain=0.0)
            else:
                # Negative sign in fast branch to match AOM lock; both PZTs have same sign.
                await self._set_pi_gains(channel=0,
                                         p_gain=-all_values[Settings.fast_p_gain],
                                         i_gain=-all_values[Settings.fast_i_gain])
                if all_values[Settings.slow_enable]:
                    await self._set_pi_gains(channel=1,
                                             p_gain=all_values[Settings.slow_p_gain],
                                             i_gain=all_values[Settings.slow_i_gain])
                else:
                    await self._set_pi_gains(channel=1, p_gain=0.0, i_gain=0.0)

        if setting in self.fast_notch_settings:
            if all_values[Settings.fast_notch_enable]:
                f0 = (all_values[Settings.fast_notch_frequency] *
                        np.pi * stabilizer.SAMPLE_PERIOD)
                q = all_values[Settings.fast_notch_quality_factor]
                # unit gain
                denominator = (1 + f0 / q + f0**2)
                a1 = 2 * (1 - f0**2) / denominator
                a2 = - (1 - f0 / q + f0**2) / denominator
                b0 = (1 + f0**2) / denominator
                b1 = - (2 * (1 - f0 ** 2)) / denominator
                await self._set_iir(channel=0, iir_idx=1, ba=[b0, b1, b0, a1, a2])
            else:
                await self._set_iir(channel=0, iir_idx=1, ba=[1.0, 0.0, 0.0, 0.0, 0.0])

        # We rely on the hardware to initialise IIR[1][1] with a simple pass-through response.

    async def _set_pi_gains(self,
                            channel: int,
                            p_gain: float,
                            i_gain: float):
        b0 = i_gain * 2 * np.pi * stabilizer.SAMPLE_PERIOD + p_gain
        b1 = -p_gain
        await self._set_iir(channel, iir_idx=0, ba=[b0, b1, 0, 1, 0])

    async def _set_iir(self, channel: int, iir_idx: int, ba: Iterable):
        key = f"{self.iir_ch_topic_base}/{channel}/{iir_idx}"
        fs = stabilizer.voltage_to_machine_units(stabilizer.DAC_FULL_SCALE)
        value = {'ba': list(ba), 'y_offset': 0.0, 'y_min': -fs, 'y_max': fs}
        await self._request_settings_change(key, value)

    def _publish_ui_change(self, topic: str, argument: Any):
        payload = json.dumps(argument).encode("utf-8")
        self._interface._client.publish(f"{self._interface._topic_base}/{topic}",
                                        payload,
                                        qos=0,
                                        retain=True)

    async def _request_settings_change(self, key: str, value: Any):
        """
        Write to the miniconf-provided topics, which currently returns a
        string message as a reply; should really be JSON/… instead, see
        quartiq/miniconf#32.
        """
        msg = await self._interface.request(key, value, retain=True)
        if starts_with(msg, "Settings fail"):
            logger.warning("Stabilizer reported failure to write setting: '%s'",
                            msg)