import argparse
import asyncio
from contextlib import suppress
from enum import Enum, unique
from gmqtt import Client as MqttClient
import json
import logging
import numpy as np
import numpy.fft as fft
import os
from PyQt5 import QtGui, QtWidgets, uic
from qasync import QEventLoop
from sipyco import common_args, pc_rpc
import sys
import time
from typing import Awaitable, Callable, List
import textwrap
import stabilizer.stream
from stabilizer import DAC_VOLTS_PER_LSB, SAMPLE_PERIOD
import threading
from collections import deque

from .mqtt import MqttInterface, StabilizerInterface, Settings
from .solstis import EnsureSolstis
from .ui_utils import link_slider_to_spinbox
from .stream_stats import StreamStats

logger = logging.getLogger(__name__)

#
# Parameters for auto-relocking algorithm.
#

#: When waiting for wavemeter readings to stabilise, wait until two consecutive readings
#: within the given number of Hz.
STABLE_READING_TOLERANCE = 5e6

#: Coarser version of STABLE_READING_TOLERANCE to use for etalon tune search, where
#: anything <100 MHz or so is completely irrelevant anyway.
ETALON_TUNE_READING_TOLERANCE = 20e6

#: Maximum laser offset from last known-good value for which to attempt reestablishing
#: the lock (rather than tuning the laser closer to the value).
MAX_RELOCK_ATTEMPT_DELTA = 3e6

#: Maximum laser offset from last known-good value for which to tune using the resonator
#: tuning control, rather than disabling the etalon lock and selecting a mode closer to
#: the target. (Must be larger than etalon step size, plus some "hysteresis".)
MAX_RESONATOR_TUNE_DELTA = 1e9

#: Response of the laser frequency to changes in the etalon tuning parameter (as per the
#: ICE-Bloc web UI), in Hz per "tune percent". Since the frequency is not continuous in
#: the parameter but rather displays steps (note aside: what are those actually – some
#: aliasing between etalon and resonator modes?), this is .
ETALON_TUNE_SLOPE = -4.68e9  # Hz per "tune percent"
ETALON_TUNE_DAMPING = 1.0

#: Response of the laser frequency to changes in the resonator tuning (as per the
#: ICE-Bloc web UI), in Hz per "tune percent".
RESONATOR_TUNE_SLOPE = 269e6

#: An extra damping factor to apply to each resonator tuning step for stability (can be
#: decreased from 1.0 if the resonator tuning search oscillating becomes an issue).
RESONATOR_TUNE_DAMPING = 1.0

#: Approximate resonator scan range (in frequency, converted using RESONATOR_TUNE_SLOPE)
#: to use for determining point at which to attempt locking. Depends on the wavemeter
#: accuracy, plus some extra slack to deal with resonator drifts during the currently
#: trivial scan algorithm.
RESONANCE_SEARCH_RADIUS = 25e6

#: Number of scan points to use when determining resonator lock point. Values too small
#: would risk missing the peak, values too large would make laser drifts affect the scan
#: too much.
RESONANCE_SEARCH_NUM_POINTS = 100

#: Fallback frequency target when a wavemeter reading with the laser in lock has not
#: been observed yet.
DEFAULT_FREQ_TARGET = 150e6

#: Interval to request wavemeter frequency readings with while not relocking.
#: Should be small enough to track (thermal) drifts in the wavemeter calibration.
IDLE_WAVEMETER_POLL_INTERVAL = 5.0

#: Interval between ADC1 reading requests while the relock task is not running.
#: Effectively lower-bounds the average re-lock response time, so should be set fairly
#: low (but not too low, as each request generates two MQTT messages).
IDLE_ADC1_POLL_INTERVAL = 0.2

#: Timeout for wavemeter requests. Should be at least a few seconds at least to
#: accommodate long exposure times on other channels, but short enough to recover from
#: lost connections while auto-relocking in a timely fashion.
WAVEMETER_TIMEOUT = 10.0

#: Number of attempts to lock at target wavemeter reading before engaging resonance
#: search.
LOCK_ATTEMPTS_BEFORE_RESONANCE_SEARCH = 5

#: Duration of a single scope trace of streamed ADC, DAC data.
#: PyQt's drawing speed and the FFT processing step limit this value.
SCOPE_TRACE_DURATION = 0.02

#: Interval between scope plot updates, in seconds.
#: PyQt's drawing speed limits value.
SCOPE_UPDATE_PERIOD = 0.05  # 20 fps

#: Time scale of quantities reported by the stream thread, in seconds.
SCOPE_TIME_SCALE = 1e-3  # Use ms and kHz as units.


@unique
class LockState(Enum):
    out_of_lock = "Out of lock"
    relocking = "Relocking"
    locked = "Locked"
    uninitialised = "Uninitialised"


class TextEditLogHandler(logging.Handler):
    def __init__(self, text_edit: QtWidgets.QTextEdit):
        super().__init__(level=logging.INFO)
        self.text_edit = text_edit
        self._text = ""
        self.formatter = logging.Formatter("<em>%(asctime)s</em>&nbsp; %(message)s",
                                           "%Y-%m-%d %H:%M:%S")

    def emit(self, record: logging.LogRecord):
        self._text += self.format(record) + "<br/>"
        self.text_edit.setText(self._text)
        self.text_edit.moveCursor(QtGui.QTextCursor.End)


class UI(QtWidgets.QMainWindow):
    afe_options = ["G1", "G2", "G5", "G10"]

    def __init__(self):
        super().__init__()

        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "app.ui")
        uic.loadUi(ui_path, self)

        self._link_paired_widgets()

        self.comm_status_label = QtWidgets.QLabel()
        self.statusbar.addPermanentWidget(self.comm_status_label)

        # Explicitly create button group to prevent shortcuts from un-selecting buttons.
        # (It's not clear to me whether this is a Qt bug or not, as the buttons are set
        # as autoExclusive (the default) in the UI file.)
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_group.addButton(self.disablePztButton)
        self.mode_group.addButton(self.rampPztButton)
        self.mode_group.addButton(self.enablePztButton)

        self.log_handler = TextEditLogHandler(self.logOutputText)
        logger.addHandler(self.log_handler)

        self.lock_state = LockState.uninitialised

        for afe in [self.afe0GainBox, self.afe1GainBox]:
            afe.addItems(self.afe_options)

        # Order is consistent with `AdcDac.to_mu()`.
        labels = ["ADC0", "ADC1", "DAC0", "DAC1"]
        scope_plot_items = [self.scopeGraphicsView.addPlot(row=i, col=j)
            for i in range(2) for j in range(2)]
        # Maximise space utilisation.
        self.scopeGraphicsView.ci.layout.setContentsMargins(0, 0, 0, 0)
        self.scopeGraphicsView.ci.layout.setSpacing(0)
        # Use legend instead of title to save space.
        legends = [plt.addLegend(offset=(-10, 10)) for plt in scope_plot_items]
        # Create the objects holding the data to plot.
        self.scope_plot_data_items = [plt.plot() for plt in scope_plot_items]
        for legend, item, title in zip(legends, self.scope_plot_data_items, labels):
            legend.addItem(item, title)

        def update_axes(val):
            if val:
                ylabel = "ASD / (V/sqrt(Hz))"
                xlabel = "Frequency / kHz"
                logmode = [True, True]
                xrange = [0.5, np.log10(0.5 * SCOPE_TIME_SCALE / SAMPLE_PERIOD)]
                yrange = [-7, -1]
            else:
                ylabel = "Amplitude / V"
                xlabel = "Time / ms"
                logmode = [False, False]
                xrange = [-SCOPE_TRACE_DURATION / SCOPE_TIME_SCALE, 0]
                yrange = [-11, 11]

            for plt in scope_plot_items:
                plt.setLogMode(*logmode)
                plt.setRange(xRange=xrange, yRange=yrange, update=False)
                plt.setLabels(left=ylabel, bottom=xlabel)

        self.enableFftBox.stateChanged.connect(update_axes)
        update_axes(self.enableFftBox.isChecked())

    def _link_paired_widgets(self):
        for s, b in [(self.fastPGainSlider, self.fastPGainBox),
                     (self.fastIGainSlider, self.fastIGainBox),
                     (self.notchFreqSlider, self.notchFreqBox),
                     (self.notchQSlider, self.notchQBox),
                     (self.slowPGainSlider, self.slowPGainBox),
                     (self.slowIGainSlider, self.slowIGainBox)]:
            link_slider_to_spinbox(s, b)

    def update_lock_state(self, state: LockState, adc1_voltage: float):
        self.lock_state = state
        color = {
            LockState.out_of_lock: "red",
            LockState.relocking: "yellow",
            LockState.locked: "green",
            LockState.uninitialised: "grey"
        }[state]
        self.adc1ReadingEdit.setStyleSheet(f"QLineEdit {{ background-color: {color} }}")
        if state == LockState.uninitialised:
            self.adc1ReadingEdit.setText("<pending>")
        else:
            self.adc1ReadingEdit.setText(f"{adc1_voltage * 1e3:0.0f} mV")

    def update_stream(self,
                      traces,
                      spectra,
                      message: str = "Waiting for stream"):
        if self.tabWidget.currentIndex() != 1:
            return
        self.streamStatusEdit.setText(message)

        data_to_show = spectra if self.enableFftBox.isChecked() else traces
        for plot, data in zip(self.scope_plot_data_items, data_to_show):
            plot.setData(*data)


async def update_stabilizer(ui: UI,
                            stabilizer_interface: StabilizerInterface,
                            root_topic: str,
                            broker_host: str,
                            local_ip: str,
                            stream_port: int,
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
                logger.warning("Unexpected value: '%s' (choices: '%s')", value, choices)

        return read, write

    def combo_box():
        def read(widgets):
            return [w.currentText() for w in widgets]

        def write(widgets, value):
            for w, v in zip(widgets, value):
                w.setCurrentIndex(UI.afe_options.index(v))

        return read, write

    # `ui/#` are only used by the UI, the others by both UI and stabilizer
    settings_map = {
        Settings.fast_p_gain: (ui.fastPGainBox, ),
        Settings.fast_i_gain: (ui.fastIGainBox, kilo),
        Settings.fast_notch_enable: (ui.notchGroup, ),
        Settings.fast_notch_frequency: (ui.notchFreqBox, kilo),
        Settings.fast_notch_quality_factor: (ui.notchQBox, ),
        Settings.slow_p_gain: (ui.slowPGainBox, ),
        Settings.slow_i_gain: (ui.slowIGainBox, ),
        Settings.slow_enable: (ui.slowPIDGroup, ),
        Settings.lock_mode: ([ui.disablePztButton, ui.rampPztButton, ui.enablePztButton],
                             radio_group(["Disabled", "RampPassThrough", "Enabled"])),
        Settings.gain_ramp_time: (ui.gainRampTimeBox, ),
        Settings.ld_threshold: (ui.lockDetectThresholdBox, ),
        Settings.ld_reset_time: (ui.lockDetectDelayBox, ),
        Settings.adc1_routing:
        ([ui.adc1IgnoreButton, ui.adc1FastInputButton, ui.adc1FastOutputButton],
         radio_group(["Ignore", "SumWithADC0", "SumWithIIR0Output"])),
        Settings.aux_ttl_out: (ui.enableAOMLockBox, invert),
        Settings.afe_gains: ([ui.afe0GainBox, ui.afe1GainBox], combo_box()),
    }

    def read_ui():
        state = {}
        for key, cfg in settings_map.items():
            read_handler = cfg[1][0] if len(cfg) == 2 else read
            state[key] = read_handler(cfg[0])
        state[Settings.stream_target] = {"ip": local_ip, "port": stream_port}
        return state

    def write_ui(key, value):
        cfg = settings_map[key]
        write_handler = cfg[1][1] if len(cfg) == 2 else write
        return write_handler(cfg[0], value)

    try:
        client = MqttClient(client_id="")
        await client.connect(broker_host, port=broker_port, keepalive=10)
        ui.comm_status_label.setText(f"Connected to MQTT broker at {broker_host}.")

        #
        # Load current settings from MQTT.
        #

        retained_settings = {}

        def collect_settings(_client, topic, value, _qos, _properties):
            subtopic = topic[len(root_topic) + 1:]
            try:
                key = Settings(subtopic)
                decoded_value = json.loads(value)
                retained_settings[key] = decoded_value
                logger.info("Registering message topic '#/%s' with value '%s'", 
                    subtopic, decoded_value)
            except:
                logger.info("Ignoring message topic '%s'", subtopic)
            return 0

        client.on_message = collect_settings
        all_settings = f"{root_topic}/#"
        client.subscribe(all_settings)
        # Based on testing, all the retained messages are sent immediately after
        # subscribing, but add some delay in case this is actually a race condition.
        await asyncio.sleep(1)
        client.unsubscribe(all_settings)
        client.on_message = lambda *a: 0

        for retained_key, retained_value in retained_settings.items():
            if retained_key in settings_map:
                write_ui(retained_key, retained_value)

        #
        # Set up UI signals.
        #

        keys_to_write = set(Settings)  # write all setings at startup
        ui_updated = asyncio.Event()
        for key, cfg in settings_map.items():
            # Capture loop variable.
            def make_queue(key):
                def queue(*args):
                    keys_to_write.add(key)
                    ui_updated.set()

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
                elif hasattr(widget, "activated"):
                    widget.activated.connect(queue)
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

        interface = MqttInterface(client, root_topic, timeout=10.0)

        # Allow relock task to directly request ADC1 updates.
        stabilizer_interface.set_interface(interface)

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
        raise


@unique
class RelockStep(Enum):
    reset_lock = "reset_lock"
    decide_next = "decide_next"
    tune_etalon = "tune_etalon"
    tune_resonator = "tune_resonator"
    determine_resonance = "determine_resonance"
    try_lock = "try_lock"


async def relock_laser(ui: UI, stabilizer_interface: StabilizerInterface,
                       get_freq: Callable[[], Awaitable[float]],
                       approximate_target_freq: float, solstis_host: str):
    logger.info(
        f"Relocking; target frequency: {(approximate_target_freq / 1e6):0.1f} MHz")

    async def get_freq_delta():
        while True:
            status, freq, _osa = await get_freq()
            if status != 0:
                # TODO: Probably downgrade to debug, as Low will be common following
                # lock loss.
                logger.info("Wavemeter returned non-zero channel status: %s", status)
                continue
            return freq - approximate_target_freq

    async def get_stable_freq_delta(tolerance=STABLE_READING_TOLERANCE):
        previous_delta = None
        while True:
            delta = await get_freq_delta()
            if previous_delta is not None:
                if abs(delta - previous_delta) < tolerance:
                    return delta
            previous_delta = delta

    async def read_adc():
        voltage = await stabilizer_interface.read_adc()
        ui.update_lock_state(LockState.relocking, voltage)
        return voltage

    async def finite_state_machine(ensure_solstis):
        # Finite state machine, exiting only through try_lock success, or cancellation.
        step = RelockStep.reset_lock

        lock_attempts_left = LOCK_ATTEMPTS_BEFORE_RESONANCE_SEARCH
        while True:
            logger.info("Current step: %s", step)
            if step == RelockStep.reset_lock:
                ui.enableAOMLockBox.setChecked(False)
                ui.disablePztButton.setChecked(True)
                await asyncio.sleep(0.1)
                step = RelockStep.decide_next
                continue
            if step == RelockStep.decide_next:
                delta = await get_stable_freq_delta()
                logger.info(f"Laser frequency delta to target: {delta / 1e6:0.1f} MHz")
                if abs(delta) < MAX_RELOCK_ATTEMPT_DELTA:
                    if lock_attempts_left > 0:
                        lock_attempts_left -= 1
                        step = RelockStep.try_lock
                    else:
                        step = RelockStep.determine_resonance
                    continue
                if abs(delta) < MAX_RESONATOR_TUNE_DELTA:
                    step = RelockStep.tune_resonator
                    continue
                step = RelockStep.tune_etalon
                continue
            if step == RelockStep.tune_etalon:
                async with ensure_solstis as solstis:
                    if solstis.etalon_locked:
                        await solstis.set_etalon_locked(False)
                    while True:
                        delta = await get_stable_freq_delta(ETALON_TUNE_READING_TOLERANCE)
                        if abs(delta) < MAX_RESONATOR_TUNE_DELTA:
                            break
                        diff = -ETALON_TUNE_DAMPING * delta / ETALON_TUNE_SLOPE
                        new_tune = solstis.etalon_tune + diff
                        logger.info("Setting etalon tune to %s", f"{new_tune:0.3f}%")
                        await solstis.set_etalon_tune(new_tune)
                    await solstis.set_etalon_locked(True)
                    step = RelockStep.decide_next
                    # Currently don't have resonator tune read-back after lock is engaged, so
                    # reopen connection completely.
                    await solstis.close()
                continue
            if step == RelockStep.tune_resonator:
                async with ensure_solstis as solstis:
                    if not solstis.etalon_locked:
                        # User unlocked etalon, frequency just happened to be right.
                        step = RelockStep.tune_etalon
                        continue
                    while True:
                        delta = await get_stable_freq_delta()
                        if abs(delta) < MAX_RELOCK_ATTEMPT_DELTA:
                            step = RelockStep.decide_next
                            break
                        if abs(delta) > MAX_RESONATOR_TUNE_DELTA:
                            # Etalon lock jumped/…
                            logger.info(
                                "Large frequency delta encountered during "
                                "resonator tuning: %s MHz", delta / 1e6)
                            step = RelockStep.tune_etalon
                            break
                        tune_step = -RESONATOR_TUNE_DAMPING * delta / RESONATOR_TUNE_SLOPE
                        new_tune = solstis.resonator_tune + tune_step
                        logger.info("Setting resonator tune to %s", f"{new_tune:0.3f}%")
                        try:
                            await solstis.set_resonator_tune(new_tune)
                        except ValueError as e:
                            logger.info(
                                "Reached invalid resonator tune; restarting from etalon search")
                            step = RelockStep.tune_etalon
                            break
                continue
            if step == RelockStep.determine_resonance:
                async with ensure_solstis as solstis:
                    if not solstis.etalon_locked:
                        # User unlocked etalon, frequency just happened to be right.
                        step = RelockStep.tune_etalon
                        continue

                    tune_centre = solstis.resonator_tune
                    radius = RESONANCE_SEARCH_RADIUS / RESONATOR_TUNE_SLOPE
                    tunes = np.linspace(tune_centre - radius, tune_centre + radius,
                                        RESONANCE_SEARCH_NUM_POINTS)[::-1]
                    adc1s = []
                    for tune in tunes:
                        await solstis.set_resonator_tune(tune, blind=True)
                        await asyncio.sleep(0.005)
                        adc1s.append(await read_adc())
                    await solstis.set_resonator_tune(tunes[np.argmax(adc1s)])
                    lock_attempts_left = LOCK_ATTEMPTS_BEFORE_RESONANCE_SEARCH
                    step = RelockStep.try_lock
                continue
            if step == RelockStep.try_lock:
                # Enable fast lock, and see if we got some transmission (allow considerably
                # lower transmission than fully locked threshold, though).
                ui.enableAOMLockBox.setChecked(True)
                await asyncio.sleep(0.2)
                transmission = await read_adc()
                if transmission < 0.2 * ui.lockDetectThresholdBox.value():
                    logger.info("Transmission immediately low (%s); aborting lock attempt",
                                f"{transmission*1e3:0.1f} mV")
                    try_approximate = False
                    step = RelockStep.reset_lock
                    continue

                # Enable PZT lock, monitor for a second during gain ramping to see if lock
                # keeps.
                ui.enablePztButton.setChecked(True)
                start = time.monotonic()
                reset = False
                while True:
                    dt = time.monotonic() - start
                    if dt > 1.0:
                        break
                    transmission = await read_adc()
                    if transmission < 0.2 * ui.lockDetectThresholdBox.value():
                        logger.info(
                            "Transmission low (%s) %s after enabling PZT lock; aborting lock attempt",
                            f"{transmission*1e3:0.1f} mV", f"{dt*1e3:0.0f} ms")
                        reset = True
                        break
                if reset:
                    step = RelockStep.reset_lock
                    continue

                # Finally, check against original criterion.
                transmission = await read_adc()
                if transmission < ui.lockDetectThresholdBox.value():
                    logger.info("Transmission not above threshold in the end")
                    step = RelockStep.reset_lock
                    continue

                logger.info("Laser relocked")
                return
            assert False, f"Unhandled step {step}"

    ensure_solstis = EnsureSolstis(solstis_host)
    try:
        await finite_state_machine(ensure_solstis)
    finally:
        await ensure_solstis.close()


class WavemeterInterface:
    """Wraps a connection to the WAnD wavemeter server, offering an interface to query
    a single channel while automatically reconnecting on failure/timeout.
    """
    def __init__(self, host: str, port: int, channel: str, timeout: float):
        self._client = None
        self._host = host
        self._port = port
        self._channel = channel
        self._timeout = timeout

    async def try_connect(self) -> None:
        try:
            self._client = pc_rpc.AsyncioClient()
            await asyncio.wait_for(self._client.connect_rpc(self._host, self._port,
                                                            "control"),
                                   timeout=self._timeout)
        except Exception:
            logger.exception("Failed to connect to WAnD server")
            self._client = None

    def is_connected(self) -> bool:
        return self._client is not None

    async def get_freq_offset(self, age=0) -> float:
        while True:
            while not self.is_connected():
                logger.info("Reconnecting to WAnD server")
                await self.try_connect()

            try:
                return await asyncio.wait_for(self._client.get_freq(laser=self._channel,
                                                                    age=age,
                                                                    priority=10,
                                                                    offset_mode=True),
                                              timeout=self._timeout)
            except Exception:
                logger.exception(f"Error getting {self._channel} wavemeter reading")
                # Drop connection (to later reconnect). In regular operation, about the
                # only reason this should happen is due to timeouts after server
                # restarts/network weirdness, so don't bother distinguishing between
                # error types.
                self._client.close_rpc()
                self._client = None


async def monitor_lock_state(ui: UI, stabilizer_interface: StabilizerInterface, wand_host: str,
                             wand_port: int, wand_channel: str, solstis_host: str):
    # While not relocking, we regularly poll the wavemeter and save the last reading,
    # to be "graduated" to last_locked_freq_reading once we know that the laser was
    # in lock for it.
    last_freq_reading = None
    last_locked_freq_reading = None

    # Connect to wavemeter server (but gracefully fail to allow using the UI for manual
    # operation even if the wavemeter is offline).
    wand = WavemeterInterface(wand_host, wand_port, wand_channel, WAVEMETER_TIMEOUT)
    await wand.try_connect()
    wavemeter_task = None
    if not wand.is_connected():
        logger.warning("Wavemeter connection not established; "
                       "automatic relocking not available")
        ui.enableRelockingBox.setChecked(False)
        ui.enableRelockingBox.setEnabled(False)
    else:
        ui.enableRelockingBox.setChecked(True)
        ui.enableRelockingBox.setEnabled(True)

        async def wavemeter_loop():
            nonlocal last_freq_reading
            while True:
                await asyncio.sleep(IDLE_WAVEMETER_POLL_INTERVAL)
                status, freq, _osa = await wand.get_freq_offset(age=0)
                if status == 0:
                    last_freq_reading = freq

        wavemeter_task = asyncio.create_task(wavemeter_loop())

    relock_task = None

    # KLUDGE: To cleanly shutdown the program, we want to be able to cancel() this task,
    # which might result in a CancelledError while we are waiting for the relock task
    # to finish – which is also what happens if the user unticks the relock enable check
    # box. To be able to disambiguate between these cases, record whether the
    # cancellation was triggered from the UI signal handler.
    we_cancelled_relock_task = False

    def relock_enable_changed(enabled):
        if not enabled and relock_task is not None:
            logger.info("Automatic relocking disabled; cancelling active relock task")
            nonlocal we_cancelled_relock_task
            we_cancelled_relock_task = True
            relock_task.cancel()

    ui.enableRelockingBox.toggled.connect(relock_enable_changed)

    try:
        while True:
            await asyncio.sleep(IDLE_ADC1_POLL_INTERVAL)
            reading = await stabilizer_interface.read_adc()
            is_locked = reading >= ui.lockDetectThresholdBox.value()
            if is_locked:
                if last_freq_reading is not None:
                    last_locked_freq_reading = last_freq_reading
            else:
                # Make sure a new frequency reading is fetched later, not one from
                # whatever might have been going on during relocking.
                last_freq_reading = None

                if ui.enableRelockingBox.isChecked():
                    assert relock_task is None
                    logger.info("Cavity transmission low (%s mV), starting relocking task.", reading * 1e3)
                    if last_locked_freq_reading is None:
                        last_locked_freq_reading = DEFAULT_FREQ_TARGET
                        logger.warning(
                            "No good frequency reference reading available, "
                            "defaulting to %s MHz", last_locked_freq_reading / 1e6)
                    relock_task = asyncio.create_task(
                        relock_laser(ui, stabilizer_interface, wand.get_freq_offset,
                                     last_locked_freq_reading, solstis_host))
                    ui.update_lock_state(LockState.relocking, reading)
                    try:
                        await relock_task
                    except asyncio.CancelledError:
                        if not we_cancelled_relock_task:
                            raise
                        we_cancelled_relock_task = False
                    relock_task = None
                    continue

            ui.update_lock_state(
                LockState.locked if is_locked else LockState.out_of_lock, reading)
    except Exception:
        logger.exception("Unexpected relocking failure")
        ui.update_lock_state(LockState.uninitialised, None)
    finally:
        if relock_task:
            relock_task.cancel()
            await relock_task
        if wavemeter_task:
            wavemeter_task.cancel()
            await wavemeter_task


def stream_worker(main_loop: asyncio.AbstractEventLoop, ui_callback: Callable,
                  terminate: threading.Event, local_ip: List[int], stream_port: int):
    """This function doesn't run in the main thread!

    The default loop on Windows doesn't support UDP!
    Also, it is not possible to change the Qt event loop. Therefore, we
    have to handle the stream in a separate thread running this function.
    """
    buffer = [deque(maxlen=int(SCOPE_TRACE_DURATION / SAMPLE_PERIOD)) for _ in range(4)]
    stat = StreamStats()

    async def handle_stream():
        """This coroutine doesn't run in the main thread's loop!"""
        bind = '.'.join(map(str, local_ip))
        transport, stream = await stabilizer.stream.StabilizerStream.open((bind, stream_port), 1)
        try:
            while not terminate.is_set():
                frame = await stream.queue.get()
                stat.update(frame)
                for buf, values in zip(buffer, frame.to_mu()):
                    buf.extend(values)
        finally:
            transport.close()

    async def handle_callback():
        """This coroutine doesn't run in the main thread's loop!"""
        while not terminate.is_set():
            while not all(map(len, buffer)):
                await asyncio.sleep(SCOPE_UPDATE_PERIOD)

            stats_message = "Speed: {:.2f} MB/s ({:.3f} % batches lost)".format(
                stat.download / 1e6, 100 * stat.loss)
            data = [np.array(buf) * DAC_VOLTS_PER_LSB for buf in buffer]
            traces = [
                (np.linspace(-len(buf) * SAMPLE_PERIOD, 0, len(buf)) / SCOPE_TIME_SCALE, buf)
                for buf in data
            ]
            transforms = [
                np.abs(fft.rfft(buf)) * np.sqrt(2 * SAMPLE_PERIOD / len(buf))
                for buf in data
            ]
            spectra = [
                (np.linspace(0, 0.5 / SAMPLE_PERIOD, len(fbuf)) * SCOPE_TIME_SCALE, fbuf)
                for fbuf in transforms
            ]
            main_loop.call_soon_threadsafe(ui_callback, traces, spectra, stats_message)
            # Do not overload the main thread!
            await asyncio.sleep(SCOPE_UPDATE_PERIOD)

    async def _wait_for_main_loop():
        """Wait until main loop is running (can only return if it is running)
        This coroutine runs in the main thread's loop.
        """
        return True

    # Wait for the future to return.
    asyncio.run_coroutine_threadsafe(_wait_for_main_loop(), main_loop).result()

    new_loop = asyncio.SelectorEventLoop()
    # Setting the event loop here only applies locally to this thread.
    asyncio.set_event_loop(new_loop)

    tasks = asyncio.gather(handle_callback(), handle_stream())
    new_loop.run_until_complete(tasks)
    new_loop.close()


class UIStatePublisher:
    def __init__(self, ui: UI):
        self.ui = ui

    async def get_lock_state(self):
        return self.ui.lock_state.name


def fmt_mac(mac: str) -> str:
    mac_nosep = ''.join(c for c in mac if c.isalnum()).lower()
    return '-'.join(textwrap.wrap(mac_nosep, 2))


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Interface for the Vescent + Stabilizer 674 laser lock setup")
    parser.add_argument("-b", "--stabilizer-broker", default="10.255.6.4")
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

        ui = UI()
        ui.show()

        stabilizer_interface = StabilizerInterface()

        ui.comm_status_label.setText(
            f"Connecting to MQTT broker at {args.stabilizer_broker}…")

        # Find out which local IP address we are going to direct the stream to.
        # Assume the local IP address is the same for the broker and the stabilizer.
        local_ip = stabilizer.stream.get_local_ip(args.stabilizer_broker)

        stabilizer_topic = f"dt/sinara/l674/{fmt_mac(args.stabilizer_mac)}"
        stabilizer_task = asyncio.create_task(
            update_stabilizer(ui, stabilizer_interface, stabilizer_topic,
                              args.stabilizer_broker, local_ip, args.stream_port))

        monitor_lock_task = asyncio.create_task(
            monitor_lock_state(ui, stabilizer_interface, args.wand_host, args.wand_port,
                               args.wand_channel, args.solstis_host))

        server = pc_rpc.Server({"l674_lock_ui": UIStatePublisher(ui)},
                               "Publishes the state of the l674-lock-ui")

        # Problem with this event loop is that `create_datagram_endpoint()`
        # used in `stabilizer.stream` is not supported on Windows.
        # https://docs.python.org/3.6/library/asyncio-eventloops.html#platform-support
        # We therefore start a new thread with a compatible event loop.
        terminate_stream = threading.Event()
        stream_thread = threading.Thread(
                target=stream_worker,
                args=(loop, ui.update_stream, terminate_stream, local_ip, args.stream_port),
        )
        stream_thread.start()

        loop.run_until_complete(
            server.start(common_args.bind_address_from_args(args), args.port))

        try:
            sys.exit(loop.run_forever())
        finally:
            terminate_stream.set()
            stream_thread.join()
            with suppress(asyncio.CancelledError):
                monitor_lock_task.cancel()
                loop.run_until_complete(monitor_lock_task)
            with suppress(asyncio.CancelledError):
                stabilizer_task.cancel()
                loop.run_until_complete(stabilizer_task)
            loop.run_until_complete(server.stop())


if __name__ == "__main__":
    main()
