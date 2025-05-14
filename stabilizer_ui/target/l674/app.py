import argparse
import asyncio
import logging
import sys

from contextlib import suppress
from gmqtt import Message as MqttMessage
from PyQt5 import QtWidgets
from qasync import QEventLoop
from sipyco import common_args, pc_rpc
from stabilizer.stream import get_local_ip

from .ui import UiWindow
from .interface import StabilizerInterface, Settings
from .lock import monitor_lock_state

from ...mqtt import MqttInterface
from ...stream.thread import StreamThread
from ...mqtt import NetworkAddress, UiMqttConfig, UiMqttBridge
from ... import mqtt
from ...utils import fmt_mac, AsyncQueueThreadsafe
from ...device_db import stabilizer_devices

logger = logging.getLogger(__name__)


async def update_stabilizer(ui: UiWindow, stabilizer_interface: StabilizerInterface,
                            root_topic: str, broker_address: NetworkAddress,
                            stream_target_queue: AsyncQueueThreadsafe[NetworkAddress]):
    invert = (lambda w: not mqtt.read(w), lambda w, v: mqtt.write(w, not v))
    kilo = (lambda w: mqtt.read(w) * 1e3, lambda w, v: mqtt.write(w, v / 1e3))

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

    # Wait for the stream thread to read the initial port.
    # A bit hacky, would ideally use a join but that seems to lead to a deadlock.
    # TODO: Get rid of this hack.
    await asyncio.sleep(1)

    # Wait for stream target to be set
    stream_target = await stream_target_queue.get()
    stream_target_queue.task_done()
    logger.debug("Got stream target from stream thread.")

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

    # Close the stream upon bad disconnect
    stream_topic = f"{root_topic}/{Settings.stream_target.value}"
    will_message = MqttMessage(stream_topic,
                               NetworkAddress.UNSPECIFIED._asdict(),
                               will_delay_interval=10)

    try:
        bridge = await UiMqttBridge.new(broker_address,
                                        settings_map,
                                        will_message=will_message)
        ui.update_comm_status(True,
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
        ui.update_comm_status(False, f"Stabilizer connection error: {text}")
        raise


class UIStatePublisher:

    def __init__(self, ui: UiWindow):
        self.ui = ui

    async def get_lock_state(self):
        return self.ui.lock_state.name


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Interface for the Vescent + Stabilizer 674 laser lock setup")
    parser.add_argument("stabilizer_name",
                        metavar="DEVICE_NAME",
                        type=str,
                        help="Stabilizer name as entered in the device database")
    parser.add_argument("--stream-port", default=9293, type=int)
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    common_args.simple_network_args(parser, 4110)
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    try:
        """
        The stabilizer config must have the additional following params:
        * "wand-address": `NetworkAddress` of the wand
        * "wand-channel": `str` of the wand channel
        * "solstis-host": `str` IPv4 of the solstis host
        """
        stabilizer = stabilizer_devices[args.stabilizer_name]
    except KeyError:
        logger.error(f"Device '{args.stabilizer_name}' not found in device database.")
        sys.exit(1)

    if stabilizer["application"] != "l674":
        logger.error(
            f"Device '{args.stabilizer_name}' is not listed as running l674 lock.")
        sys.exit(1)

    broker_address = stabilizer["broker"]

    # Find out which local IP address we are going to direct the stream to.
    # Assume the local IP address is the same for the broker and the stabilizer.
    local_ip = get_local_ip(broker_address.get_ip())
    requested_stream_target = NetworkAddress(local_ip, args.stream_port)
    net_id = stabilizer.get("net_id", fmt_mac(stabilizer["mac-address"]))
    stabilizer_topic = f"dt/sinara/l674/{net_id}"

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName("674 lock UI")

    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)

        ui = UiWindow(f"L674 lock [{args.stabilizer_name}]")
        ui.show()

        stabilizer_interface = StabilizerInterface()

        ui.update_comm_status(True,
                              f"Connecting to MQTT broker at {broker_address.get_ip()}…")

        stream_target_queue = AsyncQueueThreadsafe(maxsize=1)
        stream_target_queue.put_nowait(requested_stream_target)

        stabilizer_task = loop.create_task(
            update_stabilizer(ui, stabilizer_interface, stabilizer_topic, broker_address,
                              stream_target_queue))

        monitor_lock_task = loop.create_task(
            monitor_lock_state(ui, stabilizer_interface, stabilizer["wand-address"],
                               stabilizer["wand-channel"], stabilizer["solstis-host"]))

        server = pc_rpc.Server({"l674_lock_ui": UIStatePublisher(ui)},
                               "Publishes the state of the l674-lock-ui")

        stream_thread = StreamThread(ui.update_stream, ui.scope, stream_target_queue,
                                     broker_address, loop)

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
