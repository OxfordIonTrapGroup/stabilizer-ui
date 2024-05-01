import argparse
import asyncio
import logging
import sys
from contextlib import suppress

from PyQt5 import QtWidgets
from qasync import QEventLoop
from sipyco import common_args, pc_rpc
from stabilizer.stream import get_local_ip

from .ui import UiWindow
from .interface import StabilizerInterface, Settings
from .lock import monitor_lock_state

from ...mqtt import MqttInterface
from ...stream.fft_scope import FftScope
from ...stream.thread import StreamThread
from ...ui_mqtt_bridge import NetworkAddress, UiMqttConfig, UiMqttBridge
from ... import ui_mqtt_bridge
from ...ui_utils import fmt_mac

logger = logging.getLogger(__name__)

#: Interval between scope plot updates, in seconds.
#: PyQt's drawing speed limits value.
SCOPE_UPDATE_PERIOD = 0.05  # 20 fps


async def update_stabilizer(ui: UiWindow, stabilizer_interface: StabilizerInterface,
                            root_topic: str, broker_address: NetworkAddress,
                            stream_target: NetworkAddress):

    invert = (lambda w: not ui_mqtt_bridge.read(w),
              lambda w, v: ui_mqtt_bridge.write(w, not v))
    kilo = (lambda w: ui_mqtt_bridge.read(w) * 1e3,
            lambda w, v: ui_mqtt_bridge.write(w, v / 1e3))

    def radio_group(choices):

        def read(widgets):
            for i in range(1, len(widgets)):
                if widgets[i].isChecked():
                    return choices[i]
            # Default to first value, as Qt can sometimes transiently have all buttons
            # of a radio group disabled (apparently so when clicking an already-selected
            # button).
            return choices[0]

        def write(widgets, value):
            one_checked = False
            for widget, choice in zip(widgets, choices):
                c = choice == value
                widget.setChecked(c)
                one_checked |= c
            if not one_checked:
                logger.warning("Unexpected value: '%s' (choices: '%s')", value, choices)

        return read, write

    # `ui/#` are only used by the UI, the others by both UI and stabilizer
    settings_map = {
        Settings.fast_p_gain:
        UiMqttConfig([ui.fastPGainBox]),
        Settings.fast_i_gain:
        UiMqttConfig([ui.fastIGainBox], *kilo),
        Settings.fast_notch_enable:
        UiMqttConfig([ui.notchGroup]),
        Settings.fast_notch_frequency:
        UiMqttConfig([ui.notchFreqBox], *kilo),
        Settings.fast_notch_quality_factor:
        UiMqttConfig([ui.notchQBox]),
        Settings.slow_p_gain:
        UiMqttConfig([ui.slowPGainBox]),
        Settings.slow_i_gain:
        UiMqttConfig([ui.slowIGainBox]),
        Settings.slow_enable:
        UiMqttConfig([ui.slowPIDGroup]),
        Settings.lock_mode:
        UiMqttConfig([ui.disablePztButton, ui.rampPztButton, ui.enablePztButton],
                     *radio_group(["Disabled", "RampPassThrough", "Enabled"])),
        Settings.gain_ramp_time:
        UiMqttConfig([ui.gainRampTimeBox]),
        Settings.ld_threshold:
        UiMqttConfig([ui.lockDetectThresholdBox]),
        Settings.ld_reset_time:
        UiMqttConfig([ui.lockDetectDelayBox]),
        Settings.adc1_routing:
        UiMqttConfig(
            [ui.adc1IgnoreButton, ui.adc1FastInputButton, ui.adc1FastOutputButton],
            *radio_group(["Ignore", "SumWithADC0", "SumWithIIR0Output"])),
        Settings.aux_ttl_out:
        UiMqttConfig([ui.enableAOMLockBox], *invert),
        Settings.afe0_gain:
        UiMqttConfig([ui.afe0GainBox]),
        Settings.afe1_gain:
        UiMqttConfig([ui.afe1GainBox]),
        Settings.stream_target:
        UiMqttConfig([], lambda _: stream_target._asdict(),
                     lambda _w, _v: stream_target._asdict())
    }

    def read_ui():
        state = {}
        for key, cfg in settings_map.items():
            state[key] = cfg.read_handler(cfg.widgets)
        return state

    try:
        bridge = await UiMqttBridge.new(broker_address, settings_map)
        ui.comm_status_label.setText(
            f"Connected to MQTT broker at {broker_address.get_ip()}.")
        await bridge.load_ui(Settings, root_topic)
        keys_to_write, ui_updated = bridge.connect_ui()

        #
        # Visually enable UI.
        #

        ui.aomLockGroup.setEnabled(True)
        ui.pztLockGroup.setEnabled(True)

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
        ui.aomLockGroup.setEnabled(False)
        ui.pztLockGroup.setEnabled(False)

        text = str(e)
        if not text:
            # Show message for things like timeout errors.
            text = repr(e)
        ui.comm_status_label.setText(f"Stabilizer connection error: {text}")
        raise e


class UIStatePublisher:

    def __init__(self, ui: UiWindow):
        self.ui = ui

    async def get_lock_state(self):
        return self.ui.lock_state.name


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Interface for the Vescent + Stabilizer 674 laser lock setup")
    parser.add_argument("-b", "--broker-host", default="10.255.6.4")
    parser.add_argument("-n", "--name", default="674-lock-ui")
    parser.add_argument("--broker-port", default=1883, type=int)
    parser.add_argument("--stabilizer-mac", default="80-1f-12-5d-47-df")
    parser.add_argument("--stream-port", default=9293, type=int)
    parser.add_argument("--wand-host", default="10.255.6.61")
    parser.add_argument("--wand-port", default=3251, type=int)
    parser.add_argument("--wand-channel", default="lab1_674", type=str)
    parser.add_argument("--solstis-host", default="10.179.22.23", type=str)
    common_args.simple_network_args(parser, 4110)
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName("674 lock UI")

    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)

        ui = UiWindow(f"{args.name} [{fmt_mac(args.stabilizer_mac)}]")
        ui.show()

        stabilizer_interface = StabilizerInterface()

        ui.comm_status_label.setText(f"Connecting to MQTT broker at {args.broker_host}…")

        # Find out which local IP address we are going to direct the stream to.
        # Assume the local IP address is the same for the broker and the stabilizer.
        local_ip = get_local_ip(args.broker_host)
        stream_target = NetworkAddress(local_ip, args.stream_port)

        broker_address = NetworkAddress.from_str_ip(args.broker_host, args.broker_port)

        stabilizer_topic = f"dt/sinara/l674/{fmt_mac(args.stabilizer_mac)}"
        stabilizer_task = loop.create_task(
            update_stabilizer(ui, stabilizer_interface, stabilizer_topic, broker_address,
                              stream_target))

        monitor_lock_task = loop.create_task(
            monitor_lock_state(ui, stabilizer_interface, args.wand_host, args.wand_port,
                               args.wand_channel, args.solstis_host))

        server = pc_rpc.Server({"l674_lock_ui": UIStatePublisher(ui)},
                               "Publishes the state of the l674-lock-ui")

        stream_thread = StreamThread(ui.update_stream, FftScope.precondition_data,
                                     SCOPE_UPDATE_PERIOD, stream_target)
        stream_thread.start()

        loop.run_until_complete(
            server.start(common_args.bind_address_from_args(args), args.port))

        try:
            sys.exit(loop.run_forever())
        finally:
            stream_thread.close()
            with suppress(asyncio.CancelledError):
                monitor_lock_task.cancel()
                loop.run_until_complete(monitor_lock_task)
            with suppress(asyncio.CancelledError):
                stabilizer_task.cancel()
                loop.run_until_complete(stabilizer_task)
            loop.run_until_complete(server.stop())


if __name__ == "__main__":
    main()
