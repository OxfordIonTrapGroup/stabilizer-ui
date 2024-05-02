import logging
import asyncio

from .ui import UiWindow
from ...mqtt import UiMqttConfig, NetworkAddress, UiMqttBridge, MqttInterface
from ...interface import AbstractStabilizerInterface
from ...iir.filters import FILTERS

logger = logging.getLogger(__name__)


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Shim for controlling `dual-iir` stabilizer over MQTT
    """

    iir_ch_topic_base = "settings/iir_ch"

    async def triage_setting_change(self, setting, all_values):
        logger.info("Setting change'%s'", setting)
        if setting.split("/")[0] == "settings":
            await self.request_settings_change(setting, all_values[setting])
        else:
            self.publish_ui_change(setting, all_values[setting])
            channel, iir = setting.split("/")[1:3]
            path_root = f"ui/{channel}/{iir}/"
            y_offset = all_values[path_root + "y_offset"]
            y_min = all_values[path_root + "y_min"]
            y_max = all_values[path_root + "y_max"]
            x_offset = all_values[path_root + "x_offset"]

            filter_type = all_values[path_root + "filter"]
            filter_idx = [f.filter_type for f in FILTERS].index(filter_type)
            kwargs = {
                param: all_values[path_root + f"{filter_type}/{param}"]
                for param in FILTERS[filter_idx].parameters
            }
            ba = FILTERS[filter_idx].get_coefficients(**kwargs)

            await self.set_iir(
                channel=int(channel),
                iir_idx=int(iir),
                ba=ba,
                x_offset=x_offset,
                y_offset=y_offset,
                y_min=y_min,
                y_max=y_max,
            )

    async def update(
        self,
        ui: UiWindow,
        root_topic: str,
        broker_address: NetworkAddress,
        stream_target: NetworkAddress,
    ):
        settings_map = ui.set_mqtt_configs(stream_target)

        def read_ui():
            state = {}
            for key, cfg in settings_map.items():
                state[key] = cfg.read_handler(cfg.widgets)
            return state

        try:
            bridge = await UiMqttBridge.new(broker_address, settings_map)
            ui.set_comm_status(f"Connected to MQTT broker at {broker_address.get_ip()}.")
            await bridge.load_ui(lambda x: x, root_topic, ui)
            keys_to_write, ui_updated = bridge.connect_ui()

            #
            # Relay user input to MQTT.
            #

            interface = MqttInterface(bridge.client, root_topic, timeout=10.0)

            # Allow relock task to directly request ADC1 updates.
            self.set_interface(interface)

            # keys_to_write.update(set(Settings))
            ui_updated.set()  # trigger initial update
            while True:
                await ui_updated.wait()
                while keys_to_write:
                    # Use while/pop instead of for loop, as UI task might push extra
                    # elements while we are executing requests.
                    key = keys_to_write.pop()
                    all_params = read_ui()
                    await self.change(key, all_params)
                    await ui.update_transfer_function(key, all_params)
                ui_updated.clear()
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                return
            err_msg = str(e)
            if not err_msg:
                # Show message for things like timeout errors.
                err_msg = repr(e)
            ui.set_comm_status(f"Stabilizer connection error: {err_msg}")
            logger.exception("Failure in Stabilizer communication task")
