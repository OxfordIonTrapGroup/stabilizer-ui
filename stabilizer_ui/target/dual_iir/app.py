import argparse
import asyncio
import logging
import sys
from contextlib import suppress
from math import inf

from PyQt5 import QtWidgets
from qasync import QEventLoop
from stabilizer.stream import get_local_ip

from .ui import UiWindow
from .interface import StabilizerInterface

from ...mqtt import MqttInterface
from ...stream.thread import StreamThread
from ...mqtt import NetworkAddress, UiMqttConfig, UiMqttBridge
from ... import mqtt
from ...ui_utils import fmt_mac
from ...iir.filters import FILTERS

logger = logging.getLogger(__name__)

#: Interval between scope plot updates, in seconds.
#: PyQt's drawing speed limits value.
SCOPE_UPDATE_PERIOD = 0.05  # 20 fps
DEFAULT_WINDOW_SIZE = (1200, 600)


async def update_stabilizer(
    ui: UiWindow,
    stabilizer_interface: StabilizerInterface,
    root_topic: str,
    broker_address: NetworkAddress,
    stream_target: NetworkAddress,
):
    kilo = (
        lambda w: mqtt.read(w) * 1e3,
        lambda w, v: mqtt.write(w, v / 1e3),
    )
    kilo2 = (
        lambda w: mqtt.read(w) * 1e3,
        lambda w, v: mqtt.write(w, v / 1e3),
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

    # `ui/#` are only used by the UI, the others by both UI and stabilizer
    settings_map = {
        "settings/stream_target":
        UiMqttConfig(
            [],
            lambda _: stream_target._asdict(),
            lambda _w, _v: stream_target._asdict(),
        )
    }

    for c in range(2):
        settings_map[f"settings/afe/{c}"] = UiMqttConfig(
            [ui.channel_settings[c].afeGainBox])
        for iir in range(2):
            name_root = f"ui/{c}/{iir}/"
            iir_ui = ui.channel_settings[c].iir_widgets[iir]
            settings_map[name_root + "filter"] = UiMqttConfig([iir_ui.filterComboBox])
            settings_map[name_root + "x_offset"] = UiMqttConfig([iir_ui.x_offsetBox])
            settings_map[name_root + "y_offset"] = UiMqttConfig([iir_ui.y_offsetBox])
            settings_map[name_root + "y_max"] = UiMqttConfig([iir_ui.y_maxBox])
            settings_map[name_root + "y_min"] = UiMqttConfig([iir_ui.y_minBox])
            for f in FILTERS:
                f_str = f.filter_type
                for arg in f.parameters:
                    if arg.split("_")[-1] == "limit":
                        settings_map[name_root + f"{f_str}/{arg}"] = UiMqttConfig(
                            [
                                getattr(iir_ui.widgets[f_str], f"{arg}Box"),
                                getattr(iir_ui.widgets[f_str], f"{arg}IsInf"),
                            ],
                            *spinbox_checkbox_group(),
                        )
                    else:
                        if arg == "f0":
                            settings_map[name_root + f"{f_str}/{arg}"] = UiMqttConfig(
                                [getattr(iir_ui.widgets[f_str], f"{arg}Box")], *kilo)
                        elif arg == "Ki":
                            settings_map[name_root + f"{f_str}/{arg}"] = UiMqttConfig(
                                [getattr(iir_ui.widgets[f_str], f"{arg}Box")], *kilo)
                        elif arg == "Kii":
                            settings_map[name_root + f"{f_str}/{arg}"] = UiMqttConfig(
                                [getattr(iir_ui.widgets[f_str], f"{arg}Box")], *kilo2)
                        else:
                            settings_map[name_root + f"{f_str}/{arg}"] = UiMqttConfig(
                                [getattr(iir_ui.widgets[f_str], f"{arg}Box")])

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
        stabilizer_interface.set_interface(interface)

        # keys_to_write.update(set(Settings))
        ui_updated.set()  # trigger initial update
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
        err_msg = str(e)
        if not err_msg:
            # Show message for things like timeout errors.
            err_msg = repr(e)
        ui.set_comm_status(f"Stabilizer connection error: {err_msg}")
        logger.exception("Failure in Stabilizer communication task")


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Interface for the Dual-IIR Stabilizer.")
    parser.add_argument("-b", "--broker-host", default="10.255.6.4")
    parser.add_argument("--broker-port", default=1883, type=int)
    parser.add_argument("--stabilizer-mac", default="80-34-28-5f-59-0b")
    parser.add_argument("--stream-port", default=9293, type=int)
    parser.add_argument("--name", default="Dual IIR")
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName(f"{args.name} UI")

    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)

        ui = UiWindow(f"{args.name} [{fmt_mac(args.stabilizer_mac)}]")
        ui.resize(*DEFAULT_WINDOW_SIZE)
        ui.show()

        ui.set_comm_status(f"Connecting to MQTT broker at {args.broker_host}…")
        stabilizer_interface = StabilizerInterface()

        # Find out which local IP address we are going to direct the stream to.
        # Assume the local IP address is the same for the broker and the stabilizer.
        local_ip = get_local_ip(args.broker_host)
        stream_target = NetworkAddress(local_ip, args.stream_port)

        broker_address = NetworkAddress.from_str_ip(args.broker_host, args.broker_port)

        stabilizer_topic = f"dt/sinara/dual-iir/{fmt_mac(args.stabilizer_mac)}"
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
            ui.fft_scope.precondition_data(),
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
