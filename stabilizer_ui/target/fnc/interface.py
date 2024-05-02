import logging
import asyncio
import queue
from gmqtt import Message as MqttMessage

from . import topics
from .topics import app_root

from .ui import UiWindow
from ...mqtt import AbstractStabilizerInterface, MqttInterface
from ...ui_mqtt_bridge import UiMqttBridge, NetworkAddress, UiMqttConfig
from ...iir.filters import FILTERS

logger = logging.getLogger(__name__)


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Interface for the FNC stabilizer.
    """
    iir_ch_topic_base = topics.stabilizer.iir_root.get_path_from_root()

    async def triage_setting_change(self, setting):
        logger.info(f"Changing setting {setting.get_path_from_root()}': {setting.value}")

        setting_root = setting.root()
        if setting_root.name == "settings":
            await self.request_settings_change(setting.get_path_from_root(),
                                               setting.value)
        elif setting_root.name == "ui":
            self.publish_ui_change(setting.get_path_from_root(), setting.value)

            if (ui_iir := setting.get_parent_until(lambda x: x.name.startswith("iir"))
                ) is not None:
                await self._change_filter_setting(ui_iir)

    async def update(
        self,
        ui: UiWindow,
        broker_address: NetworkAddress,
        stream_target_queue: asyncio.Queue
    ):
        # Blocking wait until StreamThread reads the requested stream_target
        # and queues the target to be used.
        logger.info("TEMP: Waiting for stream target.")
        logger.info(f"TEMP: id in main {id(stream_target_queue)}")
        try:
            await asyncio.sleep(2)
            logger.info("TEMP: asyncio awaited.")
            # await stream_target_queue.join()
            # logger.info("TEMP: Stream target received.")
        except queue.Empty:
            logger.error("Timeout waiting for stream target.")
            raise RuntimeError("Timeout waiting for stream target.")

        stream_target = await stream_target_queue.get()
        logger.info(f"TEMP: Stream target received: {stream_target}")
        stream_target_queue.task_done()
        logger.info(f"TEMP: Stream target task done.")

        stream_target = NetworkAddress(stream_target.ip, stream_target.port)

        settings_map = ui.set_mqtt_configs(stream_target)

        def update_all_topics():
            for key, cfg in settings_map.items():
                app_root.get_child(key).value = cfg.read_handler(cfg.widgets)

        # Close the stream upon bad disconnect
        stream_topic = f"{app_root.get_path_from_root()}/{topics.stabilizer.stream_target.get_path_from_root()}"
        will_message = MqttMessage(stream_topic, NetworkAddress.UNSPECIFIED._asdict(), will_delay_interval=3)

        try:
            bridge = await UiMqttBridge.new(broker_address, settings_map, will_message=will_message)
            logger.info(f"TEMP: new bridge made")
            await bridge.load_ui(lambda x: x, app_root.get_path_from_root(), ui)
            logger.info(f"TEMP: loaded UI")
            keys_to_write, ui_updated = bridge.connect_ui()

            #
            # Relay user input to MQTT.
            #
            interface = MqttInterface(bridge.client,
                                      app_root.get_path_from_root(),
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
                    # key = keys_to_write.pop()
                    setting = app_root.get_child(keys_to_write.pop())
                    update_all_topics()
                    await self.change(setting)
                    await ui.update_transfer_function(setting)
                ui_updated.clear()
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                return
            logger.exception("Failure in Stabilizer communication task")
        finally:
            logger.info(f"Connecting to MQTT broker at {broker_address.get_ip()}.")

    async def _change_filter_setting(self, iir_setting):
        (_ch,
         _iir_idx) = int(iir_setting.get_parent().name[2:]), int(iir_setting.name[3:])

        filter_type = iir_setting.get_child("filter").value
        filters = iir_setting.get_child(filter_type)

        filter_params = {filter.name: filter.value for filter in filters.get_children()}

        ba = next(filter for filter in FILTERS
                  if filter.filter_type == filter_type).get_coefficients(**filter_params)

        await self.set_iir(
            channel=_ch,
            iir_idx=_iir_idx,
            ba=ba,
            x_offset=iir_setting.get_child("x_offset").value,
            y_offset=iir_setting.get_child("y_offset").value,
            y_min=iir_setting.get_child("y_min").value,
            y_max=iir_setting.get_child("y_max").value,
        )
