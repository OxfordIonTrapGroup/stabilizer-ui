import logging
import asyncio
from gmqtt import Message as MqttMessage
from stabilizer import DEFAULT_FNC_SAMPLE_PERIOD

from . import topics
from .topics import app_root
from .ui import UiWindow
from ...interface import AbstractStabilizerInterface
from ...mqtt import MqttInterface, UiMqttBridge, NetworkAddress, UiMqttConfig
from ...iir.filters import FILTERS

logger = logging.getLogger(__name__)


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Interface for the FNC stabilizer.
    """
    iir_ch_topic_base = topics.stabilizer.iir_root.get_path_from_root()

    def __init__(self):
        super().__init__(DEFAULT_FNC_SAMPLE_PERIOD)

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
                app_root.get_child(key).value = cfg.read_handler(cfg.widgets)

        # Close the stream upon bad disconnect
        stream_topic = f"{app_root.get_path_from_root()}/{topics.stabilizer.stream_target.get_path_from_root()}"
        will_message = MqttMessage(stream_topic, NetworkAddress.UNSPECIFIED._asdict(), will_delay_interval=3)

        try:
            bridge = await UiMqttBridge.new(broker_address, settings_map, will_message=will_message)
            ui.set_comm_status(
                f"Connected to MQTT broker at {broker_address.get_ip()}.")

            await bridge.load_ui(lambda x: x, app_root.get_path_from_root(), ui)
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
            err_msg = str(e)
            if not err_msg:
                # Show message for things like timeout errors.
                err_msg = repr(e)
            ui.set_comm_status(f"Stabilizer connection error: {err_msg}")
            logger.exception(f"Stabilizer communication failure: {err_msg}")

        finally:
            logger.info(f"Connecting to MQTT broker at {broker_address.get_ip()}.")

    async def _change_filter_setting(self, iir_setting):
        (_ch, _iir_idx) = int(iir_setting.get_parent().name[2:]), int(iir_setting.name[3:])

        filter_type = iir_setting.get_child("filter").value
        filters = iir_setting.get_child(filter_type)

        filter_params = {filter.name: filter.value for filter in filters.get_children()}

        ba = next(filter for filter in FILTERS
                  if filter.filter_type == filter_type).get_coefficients(self.sample_period, **filter_params)

        await self.set_iir(
            channel=_ch,
            iir_idx=_iir_idx,
            ba=ba,
            x_offset=iir_setting.get_child("x_offset").value,
            y_offset=iir_setting.get_child("y_offset").value,
            y_min=iir_setting.get_child("y_min").value,
            y_max=iir_setting.get_child("y_max").value,
        )
