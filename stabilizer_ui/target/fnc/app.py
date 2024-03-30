import argparse
import asyncio
import logging
import sys
from contextlib import suppress
from math import inf

from PyQt5 import QtWidgets
from qasync import QEventLoop
from stabilizer.stream import get_local_ip

from .interface import FncInterface
from .topics import app_settings_root

from ...mqtt import MqttInterface
from ...iir.channel_settings import ChannelSettings
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


class UI(QtWidgets.QMainWindow):

    def __init__(self):
        super().__init__()

        wid = QtWidgets.QWidget(self)
        self.setCentralWidget(wid)
        layout = QtWidgets.QHBoxLayout()

        # Create UI for channel settings.
        self.channel_settings = [ChannelSettings(), ChannelSettings()]

        self.tab_channel_settings = QtWidgets.QTabWidget()
        for i, channel in enumerate(self.channel_settings):
            self.tab_channel_settings.addTab(channel, f"Channel {i}")
        layout.addWidget(self.tab_channel_settings)

        # Create UI for FFT scope.
        self.fft_scope = FftScope()
        layout.addWidget(self.fft_scope)

        # Set main window layout
        wid.setLayout(layout)

    def update_stream(self, payload):
        self.fft_scope.update(payload)

    async def update_transfer_function(self, setting):
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
    stabilizer_interface: FncInterface,
    root_topic: str,
    broker_address: NetworkAddress,
    stream_target: NetworkAddress,
):
    kilo = (
        lambda w: ui_mqtt_bridge.read(w) * 1e3,
        lambda w, v: ui_mqtt_bridge.write(w, v / 1e3),
    )
    kilo2 = (
        lambda w: ui_mqtt_bridge.read(w) * 1e3,
        lambda w, v: ui_mqtt_bridge.write(w, v / 1e3),
    )

    def spinbox_checkbox_group():

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
    stabilizer_settings.get_child("stream_target").set_message(
        UiMqttConfig(
            [],
            lambda _: stream_target._asdict(),
            lambda _w, _v: stream_target._asdict(),
        ))

    for (ch_index, afe) in enumerate(stabilizer_settings.get_child("afe").get_children()):
        afe.set_message(UiMqttConfig([ui.channel_settings[ch_index].afeGainBox]))

    for (ch_index, channel) in enumerate(ui_settings.get_children()):
        for (iir_index, iir) in enumerate(ch.get_children()):
            iir_ui = ui.channel_settings[ch_index].iir_settings[iir_index]

            iir.get_child("filter").set_message(UiMqttConfig([iir_ui.filterComboBox]))
            iir.get_child("x_offset").set_message(UiMqttConfig([iir_ui.x_offsetBox]))
            iir.get_child("y_offset").set_message(UiMqttConfig([iir_ui.y_offsetBox]))
            iir.get_child("y_max").set_message(UiMqttConfig([iir_ui.y_maxBox]))
            iir.get_child("y_min").set_message(UiMqttConfig([iir_ui.y_minBox]))

            for filter in FILTERS:
                filter_topic = iir.get_child(filter.filter_type)
                for arg in filter_topic.get_children():
                    if arg.name.split("_")[-1] == "limit":
                        arg.set_message(
                            UiMqttConfig(
                                [
                                    getattr(iir_ui.widgets[filter.filter_type],
                                            f"{arg.name}Box"),
                                    getattr(iir_ui.widgets[filter.filter_type],
                                            f"{arg.name}IsInf"),
                                ],
                                *spinbox_checkbox_group(),
                            ))
                    elif arg.name in {"f0", "Ki"}:
                        arg.set_message(
                            UiMqttConfig([
                                getattr(iir_ui.widgets[filter.filter_type], f"{arg.name}Box")
                            ], *kilo))
                    elif arg.name == "Kii":
                        arg.set_message(
                            UiMqttConfig([
                                getattr(iir_ui.widgets[filter.filter_type], f"{arg.name}Box")
                            ], *kilo2))
                    else:
                        arg.set_message(
                            UiMqttConfig([
                                getattr(iir_ui.widgets[filter.filter_type], f"{arg.name}Box")
                            ]))

    settings_map = app_settings_root.traverse_as_dict()

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

        stabilizer_interface = FncInterface()

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
