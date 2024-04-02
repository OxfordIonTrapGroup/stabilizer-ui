import os
import argparse
import asyncio
import logging
import sys
from contextlib import suppress
from math import inf

from PyQt5 import QtWidgets, uic
from qasync import QEventLoop
from stabilizer.stream import get_local_ip

from .interface import StabilizerInterface
from .topics import app_settings_root
from .channels import ChannelTabWidget

from ...mqtt import MqttInterface
from ...stream.fft_scope import FftScope
from ...stream.thread import StreamThread
from ...ui_mqtt_bridge import NetworkAddress, UiMqttConfig, UiMqttBridge
from ... import ui_mqtt_bridge
from ...ui_utils import fmt_mac
from ...iir.filters import FILTERS

logger = logging.getLogger(__name__)

#: Interval between scope plot updates, in seconds.
#: PyQt's drawing speed limits value.
SCOPE_UPDATE_PERIOD = 0.05  # 20 fps

NUM_CHANNELS = 2

class UI(QtWidgets.QMainWindow):
    """ Main UI window for FNC"""
    def __init__(self):
        super().__init__()

        # Set up main window with Horizontal layout
        self.setCentralWidget(QtWidgets.QWidget(self))
        self.centralWidget = self.centralWidget()
        self.centralWidgetLayout = QtWidgets.QHBoxLayout(self.centralWidget)
        
        # Add FFT scope next to a vertical layout for settings
        self.settingsLayout = QtWidgets.QVBoxLayout()
        self.fftScopeWidget = FftScope()
        self.centralWidgetLayout.addLayout(self.settingsLayout)
        self.centralWidgetLayout.addWidget(self.fftScopeWidget)

        self.channelTabWidget = ChannelTabWidget()
        self.channels = self.channelTabWidget.channels
        self.clockWidget = uic.loadUi(os.path.join(os.path.dirname(os.path.realpath(__file__)), "widgets/clock.ui"))

        self.settingsLayout.addWidget(self.clockWidget)
        self.settingsLayout.addWidget(self.channelTabWidget)

        # Set FFT scope to take max available space
        fftScopeSizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.fftScopeWidget.setSizePolicy(fftScopeSizePolicy)
        self.fftScopeWidget.setMinimumSize(400, 200)

        self.statusbar = QtWidgets.QStatusBar(self)
        self.statusbar.setObjectName("statusbar")
        self.setStatusBar(self.statusbar)

    def update_stream(self, payload):
        self.streamWidget.update(payload)

    async def update_transfer_function(self, setting):
        """Update transfer function plot based on setting change."""
        if setting.root().name == "ui":
            ui_iir = setting.get_parent_until(lambda x: x.name.startswith("iir"))
            (_ch, _iir) = (int(ui_iir.get_parent().name[2:]), int(ui_iir.name[3:]))

            filter_type = ui_iir.get_child("filter").get_message()
            filter = ui_iir.get_child(filter_type)

            # require parameters to be set by application
            if not filter.has_children():
                raise ValueError(f"Filter {filter_type} parameter messsages not created.")
            filter_params = {f.name: f.get_message() for f in filter.get_children()}

            ba = next(f for f in FILTERS
                      if f.filter_type == filter_type).get_coefficients(**filter_params)
            _iir_settings = self.channel_settings[_ch].iir_settings[_iir]
            _iir_settings.update_transfer_function(ba)


async def update_stabilizer(
    ui: UI,
    stabilizer_interface: StabilizerInterface,
    root_topic: str,
    broker_address: NetworkAddress,
    stream_target: NetworkAddress,
):
    
    # I gains in KiloHertz
    kilo = (
        lambda w: ui_mqtt_bridge.read(w) * 1e3,
        lambda w, v: ui_mqtt_bridge.write(w, v / 1e3),
    )
    # II gains in KiloHertz^2
    kilo2 = (
        lambda w: ui_mqtt_bridge.read(w) * 1e3,
        lambda w, v: ui_mqtt_bridge.write(w, v / 1e3),
    )

    def is_inf_spinbox_checkbox_group():

        def read(widgets):
            """Expects widgets in the form [spinbox, checkbox]."""
            if widgets[1].isChecked():
                return inf
            else:
                return widgets[0].value()

        def write(widgets, value):
            """Expects widgets in the form [spinbox, checkbox]."""
            if value == inf:
                widgets[1].setChecked(True)
            else:
                widgets[0].setValue(value)

        return read, write

    stabilizer_settings, ui_settings = app_settings_root.get_children(["settings", "ui"])

    # `ui/#` are only used by the UI, the others by both UI and stabilizer
    stabilizer_settings.get_child("stream_target").set_ui_mqtt_config(
        UiMqttConfig(
            [],
            lambda _: stream_target._asdict(),
            lambda _w, _v: stream_target._asdict(),
        ))

    # Clock settings
    clk_multiplier, ext_clk, clk_freq = ui_settings.get_child("clock").get_children(["multiplier", "extClock", "frequency"])

    clk_multiplier.set_ui_mqtt_config(UiMqttConfig([ui.clockWidget.multiplierBox]))
    clk_freq.set_ui_mqtt_config(UiMqttConfig([ui.clockWidget.frequencyBox]))
    ext_clk.set_ui_mqtt_config(UiMqttConfig([ui.clockWidget.extClkCheckBox]))

    for (ch_index, channel) in enumerate(ui_settings.get_children(["ch0", "ch1"])):
        channel.get_child("afe").set_ui_mqtt_config(UiMqttConfig([ui.channels[ch_index].afeGainBox]))

        # Pounder settings
        dds_in, dds_out = channel.get_children(["pounder/ddsIn", "pounder/ddsOut"])
        
        widget_attribute = lambda dds, suffix: getattr(ui.channels[ch_index], f"{dds.name}{suffix}Box")

        for dds in (dds_in, dds_out):
            for child in dds.get_children(["attenuation", "amplitude", "frequency"]):
                child.set_ui_mqtt_config(UiMqttConfig([widget_attribute(dds, child.name.capitalize())]))
        

        for (iir_index, iir) in enumerate(channel.get_children(["iir0", "iir1"])):
            iirWidget = ui.channels[ch_index].iir_settings[iir_index]

            iir.get_child("filter").set_ui_mqtt_config(UiMqttConfig([iirWidget.filterComboBox]))
            iir.get_child("x_offset").set_ui_mqtt_config(UiMqttConfig([iirWidget.x_offsetBox]))
            iir.get_child("y_offset").set_ui_mqtt_config(UiMqttConfig([iirWidget.y_offsetBox]))
            iir.get_child("y_max").set_ui_mqtt_config(UiMqttConfig([iirWidget.y_maxBox]))
            iir.get_child("y_min").set_ui_mqtt_config(UiMqttConfig([iirWidget.y_minBox]))

            for filter in FILTERS:
                filter_topic = iir.get_child(filter.filter_type)
                for arg in filter_topic.get_children():
                    widget_attribute = lambda suffix: getattr(iirWidget.widgets[filter.filter_type], f"{arg.name}{suffix}")

                    if arg.name.split("_")[-1] == "limit":
                        arg.set_ui_mqtt_config(
                            UiMqttConfig(
                                [
                                    widget_attribute("Box"),
                                    widget_attribute("IsInf"),
                                ],
                                *is_inf_spinbox_checkbox_group(),
                            ))
                    elif arg.name in {"f0", "Ki"}:
                        arg.set_ui_mqtt_config(
                            UiMqttConfig([
                                widget_attribute("Box")
                            ], *kilo))
                    elif arg.name == "Kii":
                        arg.set_ui_mqtt_config(
                            UiMqttConfig([
                                widget_attribute("Box")
                            ], *kilo2))
                    else:
                        arg.set_ui_mqtt_config(
                            UiMqttConfig([
                                widget_attribute("Box")
                            ]))

    settings_map = {topic.get_path_from_root(): topic._ui_mqtt_config for topic in app_settings_root.get_leaves()}
    logger.error(settings_map.keys())

    def read_ui():
        state = {}
        for key, cfg in settings_map.items():
            state[key] = cfg.read_handler(cfg.widgets)
        return state

    try:
        bridge = await UiMqttBridge.new(broker_address, settings_map)
        ui.comm_status_label.setText(
            f"Connected to MQTT broker at {broker_address.get_ip()}.")
        await bridge.load_ui(lambda x: x, root_topic)
        keys_to_write, ui_updated = bridge.connect_ui()

        #
        # Relay user input to MQTT.
        #

        interface = MqttInterface(bridge.client, root_topic, timeout=10.0)

        # Allow relock task to directly request ADC1 updates.
        stabilizer_interface.set_interface(interface)

        # trigger initial update
        ui_updated.set()  
        while True:
            await ui_updated.wait()
            while keys_to_write:
                # Use while/pop instead of for loop, as UI task might push extra
                # elements while we are executing requests.
                key = keys_to_write.pop()
                all_params = read_ui()
                await stabilizer_interface.change(key, all_params)
                await ui.update_transfer_function(key, all_params)
            ui_updated.clear()
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            return
        logger.exception("Failure in Stabilizer communication task")


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Interface for the Dual-IIR Stabilizer.")
    parser.add_argument("-b", "--broker-host", default="10.255.6.4")
    parser.add_argument("--broker-port", default=1883, type=int)
    parser.add_argument("--stabilizer-mac", default="80-34-28-5f-59-0b")
    parser.add_argument("--stream-port", default=9293, type=int)
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName("Dual IIR UI")

    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)

        ui = UI()
        ui.resize(1200, 600)
        ui.show()

        stabilizer_interface = StabilizerInterface()

        # Find out which local IP address we are going to direct the stream to.
        # Assume the local IP address is the same for the broker and the stabilizer.
        local_ip = get_local_ip(args.broker_host)
        stream_target = NetworkAddress(local_ip, args.stream_port)

        broker_address = NetworkAddress(list(map(int, args.broker_host.split("."))),
                                        args.broker_port)

        stabilizer_topic = f"dt/sinara/fnc/{fmt_mac(args.stabilizer_mac)}"
        stabilizer_task = loop.create_task(
            update_stabilizer(
                ui,
                stabilizer_interface,
                stabilizer_topic,
                broker_address,
                stream_target,
            ))

        stream_thread = StreamThread(
            ui.update_stream,
            FftScope.precondition_data,
            SCOPE_UPDATE_PERIOD,
            stream_target,
            broker_address,
            loop,
        )
        stream_thread.start()

        try:
            sys.exit(loop.run_forever())
        finally:
            stream_thread.close()
            with suppress(asyncio.CancelledError):
                stabilizer_task.cancel()
                loop.run_until_complete(stabilizer_task)


if __name__ == "__main__":
    main()
