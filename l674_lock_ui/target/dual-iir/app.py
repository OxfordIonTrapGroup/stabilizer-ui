import argparse
import asyncio
import logging
import os
import sys
from contextlib import suppress
from enum import Enum, unique

import numpy as np
from PyQt5 import QtGui, QtWidgets, uic
from qasync import QEventLoop
from stabilizer.stream import get_local_ip

from .mqtt import StabilizerInterface, Settings

from ...mqtt import MqttInterface
from ...channel_settings import ChannelSettings
from ...stream.fft_scope import FftScope
from ...stream.thread import StreamThread
from ...ui_mqtt_bridge import NetworkAddress, UiMqttConfig, UiMqttBridge
from ... import ui_mqtt_bridge
from ...ui_utils import fmt_mac
from ...iir import *

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
        self.channel_settings = [('Channel 0', ChannelSettings()),
                                 ('Channel 1', ChannelSettings())]

        self.tab_channel_settings = QtWidgets.QTabWidget()
        for label, channel in self.channel_settings:
            self.tab_channel_settings.addTab(channel, label)
        layout.addWidget(self.tab_channel_settings)

        # Create UI for FFT scope.
        self.fft_scope = FftScope()
        layout.addWidget(self.fft_scope)

        # Set main window layout
        wid.setLayout(layout)

    def update_stream(self, payload):
        if self.tabWidget.currentIndex() != 1:
            return
        self.fft_scope.update(payload)


async def update_stabilizer(ui: UI, stabilizer_interface: StabilizerInterface,
                            root_topic: str, broker_address: NetworkAddress,
                            stream_target: NetworkAddress):

    # `ui/#` are only used by the UI, the others by both UI and stabilizer
    settings_map = {
        Settings.stream_target:
        UiMqttConfig([], lambda _: stream_target._asdict(),
                     lambda _w, _v: stream_target._asdict())
    }
    channels = ['0', '1']
    iirs = ['0', '1']

    filters = list(
        zip(['pid', 'notch', 'lowpass', 'highpass', 'allpass'],
            [PidArgs, NotchArgs, XPassArgs, XPassArgs, XPassArgs]))
    for c in channels:
        channel_root = f"channel_{c}_"
        settings_map[getattr(Settings, channel_root + "afe_gain")] = UiMqttConfig(
            [ui.channel_settings[int(c)][1].afeGainBox])
        for iir in iirs:
            name_root = channel_root + f"iir_{iir}_"
            iir_ui = ui.channel_settings[int(c)][1].iir_settings[int(iir)][1]
            settings_map[getattr(Settings, name_root + "filter")] = UiMqttConfig(
                [iir_ui.filterComboBox])
            settings_map[getattr(Settings, name_root + "x_offset")] = UiMqttConfig(
                [iir_ui.x_offsetBox])
            settings_map[getattr(Settings, name_root + "y_offset")] = UiMqttConfig(
                [iir_ui.y_offsetBox])
            settings_map[getattr(Settings,
                                 name_root + "y_max")] = UiMqttConfig([iir_ui.y_maxBox])
            settings_map[getattr(Settings,
                                 name_root + "y_min")] = UiMqttConfig([iir_ui.y_minBox])
            for f_str, f_args in filters:
                for arg in f_args.parameters:
                    settings_map[getattr(Settings,
                                         name_root + f"{f_str}_{arg}")] = UiMqttConfig(
                                             [getattr(iir_ui.widgets[f_str], f"{arg}Box")])

    def read_ui():
        state = {}
        for key, cfg in settings_map.items():
            state[key] = cfg.read_handler(cfg.widgets)
        return state

    try:
        bridge = await UiMqttBridge.new(broker_address, settings_map)
        # ui.comm_status_label.setText(
        #     f"Connected to MQTT broker at {broker_address.get_ip()}.")
        await bridge.load_ui(Settings, root_topic)
        keys_to_write, ui_updated = bridge.connect_ui()

        #
        # Relay user input to MQTT.
        #

        interface = MqttInterface(bridge.client, root_topic, timeout=10.0)

        # Allow relock task to directly request ADC1 updates.
        stabilizer_interface.set_interface(interface)

        keys_to_write.update(set(Settings))
        ui_updated.set()  # trigger initial update
        while True:
            await ui_updated.wait()
            while keys_to_write:
                # Use while/pop instead of for loop, as UI task might push extra
                # elements while we are executing requests.
                key = keys_to_write.pop()
                await stabilizer_interface.change(key, read_ui())
            ui_updated.clear()
    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            return
        logger.exception("Failure in Stabilizer communication task")


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Interface for the Dual-IIR Stabilizer.")
    parser.add_argument("-b", "--broker-host", default="10.255.6.4")
    parser.add_argument("--broker-port", default=1883, type=int)
    parser.add_argument("--stabilizer-mac", default="80-1f-12-5d-c8-3d")
    parser.add_argument("--stream-port", default=9293, type=int)
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName("Dual IIR UI")

    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)

        ui = UI()
        ui.resize(1200, 800)
        ui.show()

        stabilizer_interface = StabilizerInterface()

        # Find out which local IP address we are going to direct the stream to.
        # Assume the local IP address is the same for the broker and the stabilizer.
        local_ip = get_local_ip(args.broker_host)
        stream_target = NetworkAddress(local_ip, args.stream_port)

        broker_address = NetworkAddress(list(map(int, args.broker_host.split('.'))),
                                        args.broker_port)

        stabilizer_topic = f"dt/sinara/dual-iir/{fmt_mac(args.stabilizer_mac)}"
        stabilizer_task = asyncio.create_task(
            update_stabilizer(ui, stabilizer_interface, stabilizer_topic,
                              broker_address, stream_target))

        # stream_thread = StreamThread(ui.update_stream, FftScope.precondition_data,
        #                              SCOPE_UPDATE_PERIOD, stream_target)
        # stream_thread.start()

        try:
            sys.exit(loop.run_forever())
        finally:
            # stream_thread.close()
            with suppress(asyncio.CancelledError):
                stabilizer_task.cancel()
                loop.run_until_complete(stabilizer_task)


if __name__ == "__main__":
    main()
