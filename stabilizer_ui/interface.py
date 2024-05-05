import stabilizer
import asyncio
import numpy as np
import logging
import json

from typing import Any, Iterable, Optional
from sipyco import pc_rpc
from gmqtt import Message as MqttMessage

from .widgets import AbstractUiWindow
from .mqtt import MqttInterface, NetworkAddress, UiMqttBridge
from .iir.filters import get_filter
from .topic_tree import TopicTree

logger = logging.getLogger(__name__)

Y_MAX = stabilizer.voltage_to_machine_units(stabilizer.DAC_FULL_SCALE)


def starts_with(string, prefix) -> bool:
    return len(string) >= len(prefix) and string[:len(prefix)] == prefix


class AbstractStabilizerInterface:
    """
    Shim for controlling stabilizer over MQTT
    """

    stream_target_topic = "settings/stream_target"

    def __init__(self, sample_period: float, app_root: TopicTree):
        self._interface_set = asyncio.Event()
        self._interface: Optional[MqttInterface] = None
        self.sample_period = sample_period
        self.app_root = app_root
        # Default stream target topic if not specified by subclass.
        self.stream_target_topic = app_root.path() + f"/{self.stream_target_topic}"

    def set_interface(self, interface: MqttInterface) -> None:
        self._interface = interface
        self._interface_set.set()

    async def change(self, *args, **kwargs):
        await self._interface_set.wait()
        await self.triage_setting_change(*args, **kwargs)

    async def update(
        self,
        ui: AbstractUiWindow,
        broker_address: NetworkAddress,
        stream_target_queue: asyncio.Queue,
    ):
        # Wait for the stream thread to read the initial port.
        # A bit hacky, would ideally use a join but that seems to lead to a deadlock.
        # TODO: Get rid of this hack.
        await asyncio.sleep(1)

        # Wait for stream target to be set
        stream_target = await stream_target_queue.get()
        stream_target_queue.task_done()
        logger.debug("Got stream target from stream thread.")

        settings_map = ui.set_mqtt_configs(stream_target)

        def update_all_topics():
            for key, cfg in settings_map.items():
                self.app_root.child(key).value = cfg.read_handler(cfg.widgets)

        # Close the stream upon bad disconnect
        will_message = MqttMessage(self.stream_target_topic, NetworkAddress.UNSPECIFIED._asdict(), will_delay_interval=3)

        try:
            bridge = await UiMqttBridge.new(broker_address, settings_map, will_message=will_message)
            ui.set_comm_status(
                f"Connected to MQTT broker at {broker_address.get_ip()}.")

            await bridge.load_ui(lambda x: x, self.app_root.path(), ui)
            keys_to_write, ui_updated = bridge.connect_ui()

            #
            # Relay user input to MQTT.
            #
            interface = MqttInterface(bridge.client,
                                      self.app_root.path(),
                                      timeout=10.0)

            # Allow relock task to directly request ADC1 updates.
            self.set_interface(interface)

            # trigger initial update
            ui_updated.set()
            while True:
                await ui_updated.wait()
                while keys_to_write:
                    # Use while/pop instead of for loop, as UI task might push extra
                    # elements while we are executing requests.
                    setting = self.app_root.child(keys_to_write.pop())
                    update_all_topics()
                    await self.change(setting)
                    await ui.update_transfer_function(setting)
                ui_updated.clear()

        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                return
            err_msg = str(e)
            if not err_msg:
                # Show message for things like timeout errors.
                err_msg = repr(e)
            ui.set_comm_status(f"Stabilizer connection error: {err_msg}")
            logger.exception(f"Stabilizer communication failure: {err_msg}")

    async def set_pi_gains(self, channel: int, iir_idx: int, p_gain: float,
                           i_gain: float):
        b0 = i_gain * 2 * np.pi * self.sample_period + p_gain
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

    async def _change_filter_setting(self, iir_setting):
        (_ch, _iir_idx) = int(iir_setting.get_parent().name[2:]), int(iir_setting.name[3:])

        filter_type = iir_setting.child("filter").value
        filters = iir_setting.child(filter_type)

        filter_params = {filter_param.name: filter_param.value for filter_param in filters.children()}

        ba = get_filter(filter_type).get_coefficients(self.sample_period, **filter_params)

        await self.set_iir(
            channel=_ch,
            iir_idx=_iir_idx,
            ba=ba,
            x_offset=iir_setting.child("x_offset").value,
            y_offset=iir_setting.child("y_offset").value,
            y_min=iir_setting.child("y_min").value,
            y_max=iir_setting.child("y_max").value,
        )


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
