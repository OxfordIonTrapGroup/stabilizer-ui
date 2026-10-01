import stabilizer
import asyncio
import logging
import json

from typing import Any, Iterable, Optional
from gmqtt import Message as MqttMessage

from .ui import AbstractUiWindow
from .mqtt import MiniconfError, MqttInterface, NetworkAddress, UiMqttBridge
from .iir.filters import get_filter
from .topic_tree import TopicTree

logger = logging.getLogger(__name__)

Y_MAX = stabilizer.voltage_to_machine_units(stabilizer.DAC_FULL_SCALE)


class AbstractStabilizerInterface:
    """
    Shim for controlling stabilizer over MQTT
    """

    def __init__(self, sample_period: float, app_root: TopicTree):
        self._interface_set = asyncio.Event()
        self._interface: Optional[MqttInterface] = None
        self.sample_period = sample_period
        self.app_root = app_root
        self.stream_target_topic = f"{app_root.path()}/settings/stream"

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

        # Close the stream upon bad disconnect. gmqtt sends `str` payloads as they are,
        # so explicitly JSON-encode the string.
        will_message = MqttMessage(self.stream_target_topic,
                                   json.dumps(str(NetworkAddress.UNSPECIFIED)),
                                   will_delay_interval=3)

        try:
            bridge = await UiMqttBridge.new(broker_address,
                                            settings_map,
                                            will_message=will_message)
            ui.update_comm_status(
                True, f"Connected to MQTT broker at {broker_address.get_ip()}.")

            await bridge.load_ui(lambda x: x, self.app_root.path(), ui)
            keys_to_write, ui_updated = bridge.connect_ui()

            #
            # Relay user input to MQTT.
            #
            interface = MqttInterface(bridge.client,
                                      self.app_root.path(),
                                      timeout=10.0,
                                      fallback_handler=bridge.handle_status_message)
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
            ui.update_comm_status(False, f"Stabilizer connection error: {err_msg}")
            logger.exception(f"Stabilizer communication failure: {err_msg}")

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
        biquad = f"settings/ch/{channel}/biquad/{iir_idx}"
        value = {
            "coeff": {
                "ba": list(ba)
            },
            "u": stabilizer.voltage_to_machine_units(y_offset + forward_gain * x_offset),
            "min": stabilizer.voltage_to_machine_units(y_min),
            "max": stabilizer.voltage_to_machine_units(y_max),
        }
        try:
            await self._interface.request(f"{biquad}/repr/Raw", value, retain=True)
        except MiniconfError as e:
            if "Variant absent" not in e.message:
                logger.warning("Stabilizer reported failure to write setting: '%s'", e)
                return
            # The biquad is configured using a different representation (e.g. `Pid`,
            # through another client). Only switch it in this case, as writing `typ`
            # resets the biquad to the representation's default. For the same reason,
            # `typ` is not retained, as the broker does not guarantee the order in which
            # the device receives the retained messages.
            logger.info("Switching %s to the Raw representation", biquad)
            if await self.request_settings_change(f"{biquad}/typ", "Raw", retain=False):
                await self.request_settings_change(f"{biquad}/repr/Raw", value)

    async def set_setting(self, key: str, value: Any, retain: bool = False):
        """Set a device setting, raising `MiniconfError` if the device reports an error.

        Unlike `request_settings_change()`, this is not retained by default.
        """
        if self._interface is None:
            raise ConnectionError("Not connected to Stabilizer")
        await self._interface.request(key, value, retain=retain)

    async def get_setting(self, key: str) -> Any:
        """Get the value of a device setting."""
        if self._interface is None:
            raise ConnectionError("Not connected to Stabilizer")
        return await self._interface.get(key)

    def publish_ui_change(self, topic: str, argument: Any):
        payload = json.dumps(argument).encode("utf-8")
        self._interface._client.publish(f"{self._interface._topic_base}/{topic}",
                                        payload,
                                        qos=0,
                                        retain=True)

    async def request_settings_change(self,
                                      key: str,
                                      value: Any,
                                      retain: bool = True) -> bool:
        """
        Write to the miniconf-provided topics, logging any error reported by the device.

        Returns whether the device reported success.
        """
        try:
            await self._interface.request(key, value, retain=retain)
            return True
        except MiniconfError as e:
            logger.warning("Stabilizer reported failure to write setting: '%s'", e)
            return False

    async def _change_filter_setting(self, iir_setting):
        (_ch,
         _iir_idx) = int(iir_setting.get_parent().name[2:]), int(iir_setting.name[3:])

        filter_type = iir_setting.child("filter").value
        filters = iir_setting.child(filter_type)

        filter_params = {
            filter_param.name: filter_param.value
            for filter_param in filters.children()
        }

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
