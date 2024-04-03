import logging
import asyncio

from .topics import TopicTree
from . import topics

from .ui import MainWindow
from ...mqtt import AbstractStabilizerInterface, MqttInterface
from ...ui_mqtt_bridge import UiMqttBridge, NetworkAddress
from ...iir.filters import FILTERS

logger = logging.getLogger(__name__)


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Interface for the FNC stabilizer.
    """
    iir_ch_topic_base = topics.Stabilizer.iir.get_path_from_root()

    async def triage_setting_change(self, setting):
        logger.info(f"Changing setting {setting.get_path_from_root()}'")

        setting_root = setting.root()
        if setting_root.name == "settings":
            await self.request_settings_change(setting.get_path_from_root(),
                                               setting.value)
        elif setting_root.name == "ui":
            self.publish_ui_change(setting.get_path_from_root(), setting.value)

            if (ui_iir := setting.get_parent_until(lambda x: x.name.startswith("iir"))) is not None:
                await self._change_filter_setting(ui_iir)
            elif (pounder := setting.get_parent_until(lambda x: x.name == "pounder")) is not None:
                await self._change_pounder_setting(pounder)

    async def update(
        self,
        ui: MainWindow,
        root_topic: TopicTree,
        broker_address: NetworkAddress,
    ):
        topics = [topic for topic in root_topic.get_leaves() if topic._ui_mqtt_config is not None]

        settings_map = {
            topic.get_path_from_root(): topic._ui_mqtt_config
            for topic in topics
        }

        def update_all_topics():
            for topic in topics:
                topic.update_value()

        try:
            bridge = await UiMqttBridge.new(broker_address, settings_map)
            await bridge.load_ui(lambda x: x, root_topic.get_path_from_root())
            keys_to_write, ui_updated = bridge.connect_ui()

            #
            # Relay user input to MQTT.
            #
            interface = MqttInterface(bridge.client, root_topic.get_path_from_root(), timeout=10.0)

            # Allow relock task to directly request ADC1 updates.
            self.set_interface(interface)

            # trigger initial update
            ui_updated.set()
            while True:
                await ui_updated.wait()
                while keys_to_write:
                    # Use while/pop instead of for loop, as UI task might push extra
                    # elements while we are executing requests.
                    update_all_topics()
                    setting = root_topic.get_child(keys_to_write.pop())
                    
                    await self.change(setting)
                    ui.update_transfer_function(setting)
                ui_updated.clear()
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                return
            logger.exception("Failure in Stabilizer communication task")
        finally:
            logger.info(f"Connecting to MQTT broker at {broker_address.get_ip()}.")


    async def _change_filter_setting(self, iir_setting):
        (_ch, _iir_idx) = int(iir_setting.get_parent().name[2:]), int(iir_setting.name[3:])

        filter_type = iir_setting.get_child("filter").value
        filters = iir_setting.get_child(filter_type)

        filter_params = {
            filter.name: filter.value
            for filter in filters.get_children()
        }

        ba = next(
            filter for filter in FILTERS
            if filter.filter_type == filter_type).get_coefficients(**filter_params)

        await self.set_iir(
            channel=_ch,
            iir_idx=_iir_idx,
            ba=ba,
            y_offset=iir_setting.get_child("y_offset").value,
            y_min=iir_setting.get_child("y_min").value,
            y_max=iir_setting.get_child("y_max").value,
        )

    async def _change_pounder_setting(self, pounder_setting):
        ch = int(pounder_setting.get_parent().name[2:])
        topic = lambda tpc: getattr(topics.Stabilizer, tpc)

        key = topics.Stabilizer.pounder.get_path_from_root()
        clock_topics = { topic(tpc).name: topic(tpc).value for tpc in ["clk_multiplier", "ext_clk", "clk_freq"]
        }
        dds_topics = {topic.name: topic.value for dds in [topics.Stabilizer.dds_ins[ch], topics.Stabilizer.dds_outs[ch]] for topic in dds.get_children(["dds/amplitude", "dds/frequency", "attenuation"])}

        await self.request_settings_change(key, {**clock_topics, **dds_topics})