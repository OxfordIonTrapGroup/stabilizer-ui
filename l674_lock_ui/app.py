import argparse
import asyncio
from contextlib import suppress
from gmqtt import Client as MqttClient
import json
import logging
import os
from PyQt5 import QtWidgets, uic
from qasync import QEventLoop
import sys

from .ui_utils import link_slider_to_spinbox

logger = logging.getLogger(__name__)

AOM_LOCK_GPIO_IDX = 1


class UI(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "app.ui")
        uic.loadUi(ui_path, self)

        self._link_paired_widgets()

        self.comm_status_label = QtWidgets.QLabel()
        self.statusbar.addPermanentWidget(self.comm_status_label)

    def _link_paired_widgets(self):
        for s, b in [(self.fastPGainSlider, self.fastPGainBox),
                     (self.fastIGainSlider, self.fastIGainBox),
                     (self.notchFreqSlider, self.notchFreqBox),
                     (self.notchQSlider, self.notchQBox),
                     (self.slowPGainSlider, self.slowPGainBox),
                     (self.slowIGainSlider, self.slowIGainBox)]:
            link_slider_to_spinbox(s, b)


class StabilizerError(Exception):
    pass


async def update_stabilizer(ui: UI,
                            root_topic: str,
                            broker_host: str,
                            broker_port: int = 1883):
    def read(widget):
        if isinstance(widget, (
                QtWidgets.QCheckBox,
                QtWidgets.QRadioButton,
                QtWidgets.QGroupBox,
        )):
            return widget.isChecked()

        if isinstance(widget, QtWidgets.QDoubleSpinBox):
            return widget.value()

        assert f"Widget type not handled: {widget}"

    def write(widget, value):
        if isinstance(widget, (
                QtWidgets.QCheckBox,
                QtWidgets.QRadioButton,
                QtWidgets.QGroupBox,
        )):
            widget.setChecked(value)
        elif isinstance(widget, QtWidgets.QDoubleSpinBox):
            widget.setValue(value)
        else:
            assert f"Widget type not handled: {widget}"

    kilo = (lambda w: read(w) * 1e3, lambda w, v: write(w, v / 1e3))

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
                logger.warn("Unexpected value: '%s' (choices: '%s')", value, choices)

        return read, write

    settings_map = {
        "fast_gains/proportional": (ui.fastPGainBox, ),
        "fast_gains/integral": (ui.fastIGainBox, kilo),
        "fast_notch_enable": (ui.notchGroup, ),
        "fast_notch/frequency": (ui.notchFreqBox, kilo),
        "fast_notch/quality_factor": (ui.notchQBox, ),
        "slow_gains/proportional": (ui.slowPGainBox, ),
        "slow_gains/integral": (ui.slowIGainBox, ),
        "slow_enable": (ui.slowPIDGroup, ),
        "lock_mode": ([ui.disablePztButton, ui.rampPztButton, ui.enablePztButton],
                      radio_group(["Disabled", "RampPassThrough", "Enabled"])),
        "gain_ramp_time": (ui.gainRampTimeBox, ),
        "lock_detect/adc1_threshold": (ui.lockDetectThresholdBox, ),
        "lock_detect/reset_time": (ui.lockDetectDelayBox, ),
        "adc1_routing":
        ([ui.adc1IgnoreButton, ui.adc1FastInputButton, ui.adc1FastOutputButton],
         radio_group(["Ignore", "SumWithADC0", "SumWithIIR0Output"])),
        "aux_ttl_out": (ui.enableAOMLockBox, ),
    }

    # TODO: Lock detect settings.

    def read_ui(key):
        cfg = settings_map[key]
        read_handler = cfg[1][0] if len(cfg) == 2 else read
        return read_handler(cfg[0])

    def write_ui(key, value):
        cfg = settings_map[key]
        write_handler = cfg[1][1] if len(cfg) == 2 else write
        return write_handler(cfg[0], value)

    try:
        client = MqttClient(client_id="")
        await client.connect(broker_host, port=broker_port)

        #
        # Load current settings from MQTT.
        #

        settings_prefix = f"{root_topic}/settings/"
        retained_settings = {}

        def collect_settings(_client, topic, value, _qos, _properties):
            if len(topic) < len(settings_prefix) or topic[:(
                    len(settings_prefix))] != settings_prefix:
                logger.warn("Unexpected message topic: '%s'", topic)
            key = topic[len(settings_prefix):]
            retained_settings[key] = json.loads(value)
            return 0

        client.on_message = collect_settings
        all_settings = f"{settings_prefix}#"
        client.subscribe(all_settings)
        # Based on testing, all the retained messages are sent immediately after
        # subscribing, but add some delay in case this is actually a race condition.
        await asyncio.sleep(0.1)
        client.unsubscribe(all_settings)
        client.on_message = lambda *a: 0

        unexpected_settings = {}
        for retained_key, retained_value in retained_settings.items():
            if retained_key in settings_map:
                write_ui(retained_key, retained_value)
            else:
                unexpected_settings[retained_key] = retained_value

        if unexpected_settings:
            # FIXME: Not on the UI task, so shouldn't create message box here.
            QtWidgets.QMessageBox.warning(
                ui, "Unexpected settings retained in MQTT",
                "Found the following unexpected settings stored on the MQTT broker; "
                f"will delete (un-retain): {unexpected_settings}")
            for key in unexpected_settings.keys():
                client.publish(settings_prefix + key, payload=b'', qos=0, retain=True)

        #
        # Set up UI signals.
        #

        keys_to_write = set()
        updated = asyncio.Event()
        for key, cfg in settings_map.items():
            # Capture loop variable.
            def make_queue(key):
                def queue(*args):
                    keys_to_write.add(key)
                    updated.set()

                return queue

            queue = make_queue(key)

            widgets = cfg[0]
            if not isinstance(widgets, list):
                widgets = [widgets]
            for widget in widgets:
                if hasattr(widget, "valueChanged"):
                    widget.valueChanged.connect(queue)
                elif hasattr(widget, "toggled"):
                    widget.toggled.connect(queue)
                else:
                    assert False

        #
        # Visually enable UI.
        #

        ui.aomLockGroup.setEnabled(True)
        ui.pztLockGroup.setEnabled(True)

        #
        # Relay user input to MQTT.
        #

        while True:
            await updated.wait()
            for key in keys_to_write:
                topic = settings_prefix + key
                payload = json.dumps(read_ui(key)).encode("utf-8")
                client.publish(topic, payload, qos=0, retain=True)
            # FIXME: Add response topic; wait for responses here.
            keys_to_write.clear()
            updated.clear()
    except Exception as e:
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
        raise


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Interface for the Vescent + Stabilizer 674 laser lock setup")
    parser.add_argument("-b", "--broker", default="10.255.6.4")
    parser.add_argument("-t", "--topic", default="dt/sinara/stabilizer/l674")
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName("674 lock UI")

    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)

        ui = UI()
        ui.show()

        ui.comm_status_label.setText(f"Connecting to MQTT broker at {args.broker}…")
        stabilizer_task = asyncio.create_task(
            update_stabilizer(ui, args.topic, args.broker))

        try:
            sys.exit(loop.run_forever())
        finally:
            with suppress(asyncio.CancelledError):
                stabilizer_task.cancel()
                loop.run_until_complete(stabilizer_task)


if __name__ == "__main__":
    main()
