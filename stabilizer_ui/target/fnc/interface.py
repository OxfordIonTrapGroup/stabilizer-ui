import logging
import asyncio

from .topics import TopicTree
from ...mqtt import AbstractStabilizerInterface, MqttInterface
from ...ui_mqtt_bridge import UiMqttBridge, NetworkAddress
from ...iir.filters import FILTERS

logger = logging.getLogger(__name__)


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Interface for the FNC stabilizer.
    """

    async def triage_setting_change(self, setting):
        logger.info("Change setting {root}'")

        setting_root = setting.root()
        if setting_root.name == "settings":
            await self.request_settings_change(setting.get_path_from_root(),
                                               setting.get_message())
        elif setting_root.name == "ui":
            self.publish_ui_change(setting.get_path_from_root(), setting.get_message())
            ui_iir = setting.get_parent_until(lambda x: x.name.startswith("iir"))

            (_ch, _iir_idx) = int(ui_iir.get_parent().name[2:]), int(ui_iir.name[3:])

            filter_type = ui_iir.get_child("filter").get_message()
            filters = ui_iir.get_child(filter_type)

            filter_params = {
                filter.name: filter.get_message()
                for filter in filters.get_children()
            }

            ba = next(
                filter for filter in FILTERS
                if filter.filter_type == filter_type).get_coefficients(**filter_params)

            await self.set_iir(
                channel=_ch,
                iir_idx=_iir_idx,
                ba=ba,
                y_offset=ui_iir.get_child("y_offset").get_message(),
                y_min=ui_iir.get_child("y_min").get_message(),
                y_max=ui_iir.get_child("y_max").get_message(),
            )

    async def update(
        self,
        root_topic: TopicTree,
        broker_address: NetworkAddress,
    ):
        settings_map = {
            topic.get_path_from_root(): topic._ui_mqtt_config
            for topic in root_topic.get_leaves()
        }

        def read_ui():
            state = {}
            for key, cfg in settings_map.items():
                state[key] = cfg.read_handler(cfg.widgets)
            return state

        try:
            bridge = await UiMqttBridge.new(broker_address, settings_map)
            logger.info(f"Connected to MQTT broker at {broker_address.get_ip()}.")
            # self.comm_status_label.setText(
            #     f"Connected to MQTT broker at {broker_address.get_ip()}.")
            await bridge.load_ui(lambda x: x, root_topic.get_path_from_root())
            keys_to_write, ui_updated = bridge.connect_ui()

            #
            # Relay user input to MQTT.
            #
            interface = MqttInterface(bridge.client, root_topic, timeout=10.0)

            # Allow relock task to directly request ADC1 updates.
            self.set_interface(interface)

            # trigger initial update
            ui_updated.set()
            while True:
                await ui_updated.wait()
                while keys_to_write:
                    # Use while/pop instead of for loop, as UI task might push extra
                    # elements while we are executing requests.
                    key = keys_to_write.pop()
                    all_params = read_ui()
                    await self.change(key, all_params)
                    await self.update_transfer_function(key, all_params)
                ui_updated.clear()
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                return
            logger.exception("Failure in Stabilizer communication task")
        finally:
            logger.info(f"Connecting to MQTT broker at {broker_address.get_ip()}.")