import argparse
import asyncio
from enum import Enum, unique
from contextlib import suppress
from gmqtt import Client as MqttClient
import json
import logging
import os
import uuid
from PyQt5 import QtWidgets, uic
from qasync import QEventLoop
import sys
import time
from typing import Awaitable, Callable, List
from sipyco import common_args, pc_rpc

from .solstis import Solstis
from .ui_utils import link_slider_to_spinbox

logger = logging.getLogger(__name__)

AOM_LOCK_GPIO_IDX = 1


@unique
class RelockState(Enum):
    out_of_lock = "Out of lock"
    relocking = "Relocking"
    locked = "Locked"
    uninitialised = "Uninitialised"


class TextEditLogHandler(logging.Handler):
    def __init__(self, text_edit: QtWidgets.QTextEdit):
        super().__init__(level=logging.INFO)
        self.text_edit = text_edit
        self._text = ""
        self.formatter = logging.Formatter("<em>%(asctime)s</em> %(message)s",
                                           "%Y-%m-%d %H:%M:%S")

    def emit(self, record: logging.LogRecord):
        self._text += self.format(record) + "<br/>"
        self.text_edit.setText(self._text)


class UI(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "app.ui")
        uic.loadUi(ui_path, self)

        self._link_paired_widgets()

        self.comm_status_label = QtWidgets.QLabel()
        self.statusbar.addPermanentWidget(self.comm_status_label)

        # Explicitly create button group to prevent shortcuts from un-selecting buttons.
        # (It's not clear to me whether this is a Qt bug or not, as the buttons are set
        # as autoExclusive (the default) in the UI file).
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_group.addButton(self.disablePztButton)
        self.mode_group.addButton(self.rampPztButton)
        self.mode_group.addButton(self.enablePztButton)

        self.log_handler = TextEditLogHandler(self.logOutputText)
        logger.addHandler(self.log_handler)

        self.relock_state = RelockState.uninitialised

    def _link_paired_widgets(self):
        for s, b in [(self.fastPGainSlider, self.fastPGainBox),
                     (self.fastIGainSlider, self.fastIGainBox),
                     (self.notchFreqSlider, self.notchFreqBox),
                     (self.notchQSlider, self.notchQBox),
                     (self.slowPGainSlider, self.slowPGainBox),
                     (self.slowIGainSlider, self.slowIGainBox)]:
            link_slider_to_spinbox(s, b)

    def update_relock_state(self, status: RelockState):
        self.relock_state = status
        color = {
            RelockState.out_of_lock: "red",
            RelockState.relocking: "yellow",
            RelockState.locked: "green",
            RelockState.uninitialised: "grey"
        }[status]
        self.adc1ReadingEdit.setStyleSheet(f"QLineEdit {{ background-color: {color} }}")


class StabilizerError(Exception):
    pass


class ADC1ReadingQueue:
    def __init__(self):
        self._new = asyncio.Event()
        self._queue: List[asyncio.Future] = []

    async def read_adc(self) -> float:
        task = asyncio.Future()
        self._queue.append(task)
        self._new.set()
        return await task

    async def wait_for_new(self):
        await self._new.wait()
        self._new.clear()

    def pop_requests(self) -> List[asyncio.Future]:
        queue = self._queue
        self._queue = []
        return queue


async def update_stabilizer(ui: UI,
                            adc1_requests: ADC1ReadingQueue,
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

    invert = (lambda w: not read(w), lambda w, v: write(w, not v))
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
        "aux_ttl_out": (ui.enableAOMLockBox, invert),
    }

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
        ui.comm_status_label.setText(f"Connected to MQTT broker at {broker_host}.")

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
        await asyncio.sleep(1)
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
        # Set up auto-reloading ADC1 querying.
        #

        adc1_request_topic = "dt/sinara/stabilizer/l674/read_adc1_filtered"
        client_id = str(uuid.uuid4()).split("-")[0]
        adc1_response_topic = f"dt/sinara/stabilizer/l674/response_{client_id}/adc1_filtered"
        client.subscribe(adc1_response_topic)

        pending_adc1_requests = []

        def handle_message(_client, topic, value, _qos, _properties):
            if topic == adc1_response_topic:
                voltage = float(value)
                ui.adc1ReadingEdit.setText(f"{voltage * 1e3:0.0f} mV")
                req = pending_adc1_requests.pop(0)
                req.set_result(voltage)
            else:
                logger.warning("Unexpected MQTT message topic: %s", topic)

        client.on_message = handle_message

        def request_adc1_reading():
            client.publish(adc1_request_topic,
                           b'',
                           qos=0,
                           retain=False,
                           response_topic=adc1_response_topic)

        #
        # Relay user input to MQTT.
        #

        while True:
            await asyncio.wait([
                asyncio.create_task(t)
                for t in [updated.wait(), adc1_requests.wait_for_new()]
            ],
                               return_when=asyncio.FIRST_COMPLETED)
            for key in keys_to_write:
                topic = settings_prefix + key
                payload = json.dumps(read_ui(key)).encode("utf-8")
                client.publish(topic, payload, qos=0, retain=True)
            # FIXME: Add response topic; complain if nothing/error returned.
            keys_to_write.clear()
            updated.clear()

            for req in adc1_requests.pop_requests():
                pending_adc1_requests.append(req)
                request_adc1_reading()

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


STABLE_READING_TOLERANCE = 5e6
ETALON_TUNE_READING_TOLERANCE = 20e6
RELOCK_FREQ_TOLERANCE = 3e6
ETALON_FREQ_TOLERANCE = 500e6
ETALON_TUNE_SLOPE = -4.68e9  # Hz per "tune percent"
ETALON_TUNE_DAMPING = 0.95
RESONATOR_TUNE_SLOPE = 269e6  # Hz per "tune percent"
RESONATOR_TUNE_DAMPING = 0.95


@unique
class RelockStep(Enum):
    reset_lock = "reset_lock"
    decide_next = "decide_next"
    tune_etalon = "tune_etalon"
    tune_resonator = "tune_resonator"
    try_lock = "try_lock"


async def relock_laser(ui: UI, adc1_request_queue: ADC1ReadingQueue,
                       get_freq: Callable[[], Awaitable[float]], target_freq: float,
                       solstis_host: str):
    async def get_freq_delta():
        while True:
            status, freq, _osa = await get_freq()
            if status != 0:
                # TODO: Probably downgrade to debug, as Low will be common following
                # lock loss.
                logger.info("Wavemeter returned non-zero channel status: %s", status)
                continue
            return freq - target_freq

    async def get_stable_freq_delta(tolerance=STABLE_READING_TOLERANCE):
        previous_delta = None
        while True:
            delta = await get_freq_delta()
            if previous_delta is not None:
                if abs(delta - previous_delta) < tolerance:
                    return delta
            previous_delta = delta

    solstis = None

    async def ensure_solstis():
        nonlocal solstis
        if solstis is None:
            solstis = await Solstis.new(solstis_host)
        return solstis

    # Finite state machine, exiting only through try_lock success, or cancellation.
    step = RelockStep.reset_lock
    while True:
        logger.info("Current step: %s", step)
        if step == RelockStep.reset_lock:
            ui.enableAOMLockBox.setChecked(False)
            ui.disablePztButton.setChecked(True)
            step = RelockStep.decide_next
            continue
        if step == RelockStep.decide_next:
            delta = await get_stable_freq_delta()
            logger.info("Laser frequency delta: %s MHz", delta / 1e6)
            if abs(delta) < RELOCK_FREQ_TOLERANCE:
                step = RelockStep.try_lock
                continue
            if abs(delta) < ETALON_FREQ_TOLERANCE:
                step = RelockStep.tune_resonator
                continue
            step = RelockStep.tune_etalon
            continue
        if step == RelockStep.tune_etalon:
            await ensure_solstis()
            if solstis.etalon_locked:
                await solstis.set_etalon_locked(False)
            while True:
                delta = await get_stable_freq_delta(ETALON_TUNE_READING_TOLERANCE)
                if abs(delta) < ETALON_FREQ_TOLERANCE:
                    break
                diff = -ETALON_TUNE_DAMPING * delta / ETALON_TUNE_SLOPE
                new_tune = solstis.etalon_tune + diff
                logger.info("Setting etalon tune to %s", new_tune)
                await solstis.set_etalon_tune(new_tune)
            await solstis.set_etalon_locked(True)
            # Currently don't have resonator tune readback after lock is engaged, so re-open connection completely.
            await solstis.close()
            solstis = None
            step = RelockStep.decide_next
            continue
        if step == RelockStep.tune_resonator:
            await ensure_solstis()
            if not solstis.etalon_locked:
                # User unlocked etalon, frequency just happened to be right.
                step = RelockStep.tune_etalon
                continue

            while True:
                delta = await get_stable_freq_delta()
                if abs(delta) < RELOCK_FREQ_TOLERANCE:
                    step = RelockStep.decide_next
                    break
                if abs(delta) > ETALON_FREQ_TOLERANCE:
                    # Etalon lock jumped/…
                    logger.info(
                        "Large frequency delta encountered during "
                        "resonator tuning: %s MHz", delta / 1e6)
                    step = RelockStep.tune_etalon
                    break
                step = -RESONATOR_TUNE_DAMPING * delta / RESONATOR_TUNE_SLOPE
                new_tune = solstis.resonator_tune + step
                logger.info("Setting resonator tune to %s", new_tune)
                await solstis.set_resonator_tune(new_tune)
            continue
        if step == RelockStep.try_lock:
            # Enable fast lock, and see if we got some transmission (allow considerably
            # lower transmision than fully locked threshold, though).
            ui.enableAOMLockBox.setChecked(True)
            await asyncio.sleep(0.1)
            transmission = await adc1_request_queue.read_adc()
            if transmission < 0.2 * ui.lockDetectThresholdBox.value():
                logger.info("Transmission immediately low; aborting lock attempt")
                step = RelockStep.reset_lock
                continue

            # Enable PZT lock, monitor for a second during gain ramping to see if lock
            # keeps.
            ui.enablePztButton.setChecked(True)
            start = time.monotonic()
            while True:
                dt = time.monotonic() - start
                if dt > 1.0:
                    break
                transmission = await adc1_request_queue.read_adc()
                if transmission < 0.2 * ui.lockDetectThresholdBox.value():
                    logger.info(
                        "Transmission low after enable PZT lock (%s s); "
                        "aborting lock attempt", dt)
                    step = RelockStep.reset_lock
                    continue

            # Finally, check against original criterion.
            transmission = await adc1_request_queue.read_adc()
            if transmission < ui.lockDetectThresholdBox.value():
                logger.info("Transmission not above threshold in the end")
                step = RelockStep.reset_lock
                continue

            logger.info("Laser relocked")
            return


async def monitor_lock_state(ui: UI, adc1_request_queue: ADC1ReadingQueue,
                             wand_host: str, wand_port: int, wand_channel: str,
                             solstis_host: str):
    last_freq_reading = None
    last_locked_freq_reading = None
    wand = None
    try:
        wand = pc_rpc.AsyncioClient()
        await asyncio.wait_for(wand.connect_rpc(wand_host, wand_port, "control"),
                               timeout=10.0)
    except Exception:
        logger.exception("Failed to connect to WAnD server")
        wand = None

    if wand is None:
        logger.warning("Wavemeter connection not established; "
                       "automatic relocking not available")
        ui.enableRelockingBox.setChecked(False)
        ui.enableRelockingBox.setEnabled(False)
    else:
        ui.enableRelockingBox.setChecked(True)
        ui.enableRelockingBox.setEnabled(True)

        async def get_freq(age=0):
            return await wand.get_freq(laser=wand_channel,
                                       age=age,
                                       priority=10,
                                       offset_mode=True)

        async def wavemeter_loop():
            nonlocal last_freq_reading
            while True:
                await asyncio.sleep(5.0)
                try:
                    status, freq, _osa = await get_freq(age=1.0)
                except Exception as e:
                    logger.warning("Failed to get wavemeter reading: %s", repr(e))
                    continue
                if status == 0:
                    last_freq_reading = freq

        # TODO: Cancle this task on shutdown too.
        asyncio.create_task(wavemeter_loop())

    relock_task = None

    we_cancelled = False

    def relock_enable_changed(enabled):
        if not enabled and relock_task is not None:
            logger.info("Automatic relocking disabled; cancelling active relock task")
            nonlocal we_cancelled
            we_cancelled = True
            relock_task.cancel()

    ui.enableRelockingBox.toggled.connect(relock_enable_changed)

    try:
        while True:
            await asyncio.sleep(0.1)
            reading = await adc1_request_queue.read_adc()
            is_locked = reading >= ui.lockDetectThresholdBox.value()
            if is_locked:
                if last_freq_reading is not None:
                    last_locked_freq_reading = last_freq_reading
            else:
                # Make sure a new frequency reading is fetched later, not one from whatever
                # might have been going on during relocking.
                last_freq_reading = None

                if ui.enableRelockingBox.isChecked():
                    assert relock_task is None
                    if last_locked_freq_reading is None:
                        last_locked_freq_reading = 140e6
                        logger.warning(
                            "No good frequency reference reading available, "
                            "defaulting to %s MHz", last_locked_freq_reading / 1e6)
                    relock_task = asyncio.create_task(
                        relock_laser(ui, adc1_request_queue, get_freq,
                                     last_locked_freq_reading, solstis_host))
                    ui.update_relock_state(RelockState.relocking)
                    try:
                        await relock_task
                    except asyncio.CancelledError:
                        if not we_cancelled:
                            raise
                        we_cancelled = False
                    relock_task = None
                    logger.info("Relock task finished.")
                    continue

            ui.update_relock_state(
                RelockState.locked if is_locked else RelockState.out_of_lock)
    except Exception:
        logger.exception("Unexpected relocking failure")
        ui.update_relock_state(RelockState.uninitialised)


class UIStatePublisher:
    def __init__(self, ui: UI):
        self.ui = ui

    async def get_relock_state(self):
        return self.ui.relock_state.name


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Interface for the Vescent + Stabilizer 674 laser lock setup")
    parser.add_argument("-b", "--stabilizer-broker", default="10.255.6.4")
    parser.add_argument("--stabilizer-topic", default="dt/sinara/stabilizer/l674")
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

        ui = UI()
        ui.show()

        adc1_request_queue = ADC1ReadingQueue()

        ui.comm_status_label.setText(
            f"Connecting to MQTT broker at {args.stabilizer_broker}…")
        stabilizer_task = asyncio.create_task(
            update_stabilizer(ui, adc1_request_queue, args.stabilizer_topic,
                              args.stabilizer_broker))

        monitor_lock_task = asyncio.create_task(
            monitor_lock_state(ui, adc1_request_queue, args.wand_host, args.wand_port,
                               args.wand_channel, args.solstis_host))

        server = pc_rpc.Server({'674LockUIState': UIStatePublisher(ui)},
                               "Publishes the state of the l674-lock-ui")
        loop.run_until_complete(
            server.start(common_args.bind_address_from_args(args), args.port))

        try:
            sys.exit(loop.run_forever())
        finally:
            with suppress(asyncio.CancelledError):
                monitor_lock_task.cancel()
                loop.run_until_complete(monitor_lock_task)
            with suppress(asyncio.CancelledError):
                stabilizer_task.cancel()
                loop.run_until_complete(stabilizer_task)
            loop.run_until_complete(server.stop())


if __name__ == "__main__":
    main()
