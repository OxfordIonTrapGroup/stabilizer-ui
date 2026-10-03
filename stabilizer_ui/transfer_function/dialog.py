"""Window for measuring, comparing and saving transfer functions."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters
from PyQt6 import QtCore, QtGui, QtWidgets
from scipy import signal

from . import ess
from .measurement import (Measurement, SweepRunner, PRE_TRIGGER, post_trigger_duration)
from ..plot import COLOURS, FrequencyAxis, GraphicsLayoutWidget, format_frequency
from ..scientific_spinbox import ScientificSpinBox

logger = logging.getLogger(__name__)

#: Sweep parameters remembered between sessions (QSettings key, default).
DEFAULTS = {
    "channel": 0,
    "f_start": 100.0,
    "f_stop": 300e3,
    "duration": 1.0,
    "amplitude": 0.1,
    "runs": 1,
    "auto_window": True,
    "ir_window": 100.0,
    "points_per_decade": 100,
    "harmonics": 2,
}

#: Harmonics are shown in the Bode plot where they exceed this multiple of their noise.
HARMONIC_THRESHOLD = 3

#: Line styles of the harmonics in the Bode plot.
HARMONIC_STYLES = {
    2: QtCore.Qt.PenStyle.DashDotLine,
    3: QtCore.Qt.PenStyle.DashDotDotLine,
    4: [1, 4],
}

#: Opacity (out of 255) of the area below the estimated noise.
NOISE_ALPHA = 50


def _frequency_box(value: float) -> ScientificSpinBox:
    box = ScientificSpinBox()
    box.setDecimals(3)
    box.setSigFigs(4)
    box.setRange(0.01, 1e6)
    box.setSuffix(" Hz")
    box.setValue(value)
    return box


def _db(x):
    with np.errstate(divide="ignore", invalid="ignore"):
        return 20 * np.log10(np.abs(x))


class _NoiseShading(pg.PlotDataItem):
    """Shades the area below a curve (an estimated noise) in a translucent colour.

    The area extends to the bottom of the view, following it as the view changes. Only
    the curve counts towards the auto range, which would otherwise never move up again.
    Non-finite points are left out (the shading bridges them).
    """

    def __init__(self, x, y, colour):
        brush = pg.mkColor(colour)
        brush.setAlpha(NOISE_ALPHA)
        super().__init__(x, y, pen=None, brush=brush, fillLevel=0.0)
        # Below the curves, which would otherwise be tinted by the shading.
        self.setZValue(-1)

    def viewRangeChanged(self, vb=None, ranges=None, changed=None):
        super().viewRangeChanged(vb, ranges, changed)
        # (Outside a `ViewBox`, e.g. while being removed, this is the `GraphicsView`.)
        view = self.getViewBox()
        if isinstance(view, pg.ViewBox):
            self.setFillLevel(view.viewRange()[1][0])

    def dataBounds(self, ax, frac=1.0, orthoRange=None):
        if ax == 0:
            return super().dataBounds(ax, frac, orthoRange)
        x, y = self.getData()
        if y is None:
            return (None, None)
        if orthoRange is not None:
            y = y[(x >= orthoRange[0]) & (x <= orthoRange[1])]
        y = y[np.isfinite(y)]
        if not len(y):
            return (None, None)
        lower, upper = np.percentile(y, [50 * (1 - frac), 50 * (1 + frac)])
        return float(lower), float(upper)


def _phase(value: np.ndarray, unwrap: bool = True) -> np.ndarray:
    """Phase in degrees, within (-180, 180] or, if unwrapped, starting there."""
    phase = np.angle(value)
    if not unwrap:
        return np.degrees(phase)
    phase = np.degrees(np.unwrap(phase))
    if len(phase):
        phase -= 360 * np.round(phase[0] / 360)
    return phase


def _crossover(f: np.ndarray, loop_gain: np.ndarray):
    """First unity gain (crossover) frequency of a loop gain, and the phase margin."""
    magnitude = np.abs(loop_gain)
    crossings = np.flatnonzero((magnitude[:-1] >= 1) & (magnitude[1:] < 1))
    if not len(crossings):
        return None
    i = crossings[0]
    log_f, log_m = np.log(f[i:i + 2]), np.log(magnitude[i:i + 2])
    t = -log_m[0] / (log_m[1] - log_m[0])
    phase = np.unwrap(np.angle(loop_gain[i:i + 2]))
    margin = math.degrees(phase[0] + t * (phase[1] - phase[0])) + 180
    margin = (margin + 180) % 360 - 180
    return math.exp(log_f[0] + t * (log_f[1] - log_f[0])), margin


class TransferFunctionWindow(QtWidgets.QDialog):
    """Runs transfer function measurements with exponential sine sweeps, and shows,
    compares and saves the results."""

    def __init__(self, runner: SweepRunner, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Transfer Function [{runner.device}]")
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint)
        self.runner = runner
        self.measurements: list[Measurement] = []
        self._colours: dict[int, str] = {}
        self._next_colour = 0
        self._task: asyncio.Task | None = None
        self._data_cache = None

        layout = QtWidgets.QHBoxLayout(self)
        splitter = QtWidgets.QSplitter()
        layout.addWidget(splitter)
        # Scroll the controls if the window is too small for them.
        controls = QtWidgets.QScrollArea()
        controls.setWidget(self._make_controls())
        controls.setWidgetResizable(True)
        controls.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        controls.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        splitter.addWidget(controls)
        splitter.addWidget(self._make_plots())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 970])
        self.resize(1300, 800)
        # Enter is handled by `keyPressEvent()` instead, depending on the field.
        for button in self.findChildren(QtWidgets.QPushButton):
            button.setAutoDefault(False)

        self._load_parameters()
        self._update_sweep_info()
        self._update_buttons()

    def keyPressEvent(self, event):
        # Enter in a sweep parameter runs a measurement, and in an analysis parameter
        # re-analyses the selected one.
        if event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            focus = self.focusWidget()
            for group, button in [(self.sweep_group, self.run_button),
                                  (self.analysis_group, self.reanalyse_button)]:
                if focus is not None and group.isAncestorOf(focus):
                    if button.isEnabled():
                        button.click()
                    return
        super().keyPressEvent(event)

    #
    # Layout.
    #

    def _make_controls(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.sweep_group = QtWidgets.QGroupBox("Sweep")
        form = QtWidgets.QFormLayout(self.sweep_group)
        self.channel_box = QtWidgets.QComboBox()
        self.channel_box.addItems(["0", "1"])
        self.channel_box.setToolTip(
            "The sweep is added to the DAC output of this channel")
        form.addRow("Channel:", self.channel_box)
        self.start_box = _frequency_box(DEFAULTS["f_start"])
        form.addRow("Start:", self.start_box)
        self.stop_box = _frequency_box(DEFAULTS["f_stop"])
        form.addRow("Stop:", self.stop_box)
        self.duration_box = QtWidgets.QDoubleSpinBox()
        self.duration_box.setRange(0.01, 20)
        self.duration_box.setDecimals(2)
        self.duration_box.setSingleStep(0.1)
        self.duration_box.setSuffix(" s")
        self.duration_box.setToolTip(
            "Longer sweeps reduce the noise, separate the harmonics further, and allow "
            "lower start frequencies")
        form.addRow("Duration:", self.duration_box)
        self.amplitude_box = QtWidgets.QDoubleSpinBox()
        self.amplitude_box.setRange(1e-3, 10)
        self.amplitude_box.setDecimals(3)
        self.amplitude_box.setSingleStep(0.01)
        self.amplitude_box.setSuffix(" V")
        self.amplitude_box.setToolTip(
            "The sweep is added to the filter output, so the sum needs to stay within "
            "the DAC range (and the linear range of the system)")
        form.addRow("Amplitude:", self.amplitude_box)
        self.runs_box = QtWidgets.QSpinBox()
        self.runs_box.setRange(1, 100)
        self.runs_box.setToolTip("Number of sweeps to average")
        form.addRow("Averages:", self.runs_box)
        self.sweep_info = QtWidgets.QLabel()
        self.sweep_info.setWordWrap(True)
        form.addRow(self.sweep_info)
        hint = QtWidgets.QLabel(
            "Hold the channel to measure the plant open-loop. With the loop closed, "
            "the plant, loop gain and controller are measured in situ.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        form.addRow(hint)
        layout.addWidget(self.sweep_group)

        self.analysis_group = QtWidgets.QGroupBox("Analysis")
        form = QtWidgets.QFormLayout(self.analysis_group)
        window_layout = QtWidgets.QHBoxLayout()
        self.window_box = QtWidgets.QDoubleSpinBox()
        self.window_box.setRange(0.1, 1e4)
        self.window_box.setDecimals(1)
        self.window_box.setSuffix(" ms")
        self.window_box.setToolTip(
            "Length of the impulse response window. This sets the frequency resolution, "
            "and thus the lowest usable frequency (results within about the first octave "
            "of the start frequency are less accurate), but longer windows collect more "
            "noise.")
        self.auto_window_box = QtWidgets.QCheckBox("Auto")
        self.auto_window_box.setToolTip("Ten periods of the start frequency")
        window_layout.addWidget(self.window_box, 1)
        window_layout.addWidget(self.auto_window_box)
        form.addRow("IR window:", window_layout)
        self.points_box = QtWidgets.QSpinBox()
        self.points_box.setRange(10, 2000)
        self.points_box.setToolTip(
            "Points per decade; the responses are averaged over the interval each point "
            "represents, so fewer points mean more smoothing")
        form.addRow("Points/decade:", self.points_box)
        self.harmonics_box = QtWidgets.QSpinBox()
        self.harmonics_box.setRange(0, 3)
        self.harmonics_box.setToolTip(
            "Number of harmonics (2nd, 3rd, …) to show, to estimate the linearity. They "
            "are plotted against the fundamental frequency (the harmonic itself is at k "
            "times that), normalised like the fundamental response (so that their "
            "distance to it is the harmonic distortion). In the Bode plot, they are only "
            f"shown where they exceed {HARMONIC_THRESHOLD} times their estimated noise.")
        form.addRow("Harmonics:", self.harmonics_box)
        self.reanalyse_button = QtWidgets.QPushButton("Re-analyse selected")
        form.addRow(self.reanalyse_button)
        layout.addWidget(self.analysis_group)

        run_layout = QtWidgets.QHBoxLayout()
        self.run_button = QtWidgets.QPushButton("Run")
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        run_layout.addWidget(self.run_button)
        run_layout.addWidget(self.cancel_button)
        layout.addLayout(run_layout)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        list_group = QtWidgets.QGroupBox("Measurements")
        list_layout = QtWidgets.QVBoxLayout(list_group)
        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setToolTip(
            "Checked measurements are shown in the Bode plot; the selected one in the "
            "other tabs. Double-click to rename.")
        list_layout.addWidget(self.list_widget)
        buttons = QtWidgets.QGridLayout()
        self.load_button = QtWidgets.QPushButton("Load…")
        self.save_button = QtWidgets.QPushButton("Save…")
        self.csv_button = QtWidgets.QPushButton("Export CSV…")
        self.plot_button = QtWidgets.QPushButton("Export plot…")
        self.remove_button = QtWidgets.QPushButton("Remove")
        for i, button in enumerate([
                self.load_button, self.save_button, self.csv_button, self.plot_button,
                self.remove_button
        ]):
            buttons.addWidget(button, i // 2, i % 2)
        list_layout.addLayout(buttons)
        layout.addWidget(list_group, 1)

        for box in [self.start_box, self.stop_box, self.duration_box, self.amplitude_box]:
            box.valueChanged.connect(self._update_sweep_info)
        self.auto_window_box.toggled.connect(self._update_sweep_info)
        self.window_box.valueChanged.connect(self._update_sweep_info)
        self.run_button.clicked.connect(self._run)
        self.cancel_button.clicked.connect(self._cancel)
        self.reanalyse_button.clicked.connect(self._reanalyse)
        self.load_button.clicked.connect(self._load)
        self.save_button.clicked.connect(self._save)
        self.csv_button.clicked.connect(self._export_csv)
        self.plot_button.clicked.connect(self._export_plot)
        self.remove_button.clicked.connect(self._remove)
        self.list_widget.itemChanged.connect(self._item_changed)
        self.list_widget.currentRowChanged.connect(self._selection_changed)
        # Delete (or Backspace, labelled delete on Mac keyboards) in the list removes the
        # selected measurement, unless it is being renamed.
        for key in [QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace]:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(key), self.list_widget)
            shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self.remove_button.click)
        return panel

    def _make_plots(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs)

        # Bode plot.
        bode = QtWidgets.QWidget()
        bode_layout = QtWidgets.QVBoxLayout(bode)
        options = QtWidgets.QHBoxLayout()
        options.addWidget(QtWidgets.QLabel("Show:"))
        self.quantity_box = QtWidgets.QComboBox()
        for quantity, label in ess.QUANTITIES.items():
            self.quantity_box.addItem(label.format(n="n", m="m"), quantity)
        self.quantity_box.setToolTip(
            "s: stimulus, y: filter output (DAC without the stimulus), n: excited "
            "channel, m: other channel")
        options.addWidget(self.quantity_box, 1)
        self.noise_check = QtWidgets.QCheckBox("Noise")
        self.noise_check.setChecked(True)
        self.noise_check.setToolTip("Shade the area below the estimated noise (1σ)")
        options.addWidget(self.noise_check)
        self.unwrap_check = QtWidgets.QCheckBox("Unwrap phase")
        self.unwrap_check.setChecked(True)
        self.unwrap_check.setToolTip(
            "Unwrap the phase over frequency instead of showing it within ±180°. Noisy "
            "data (e.g. below the corner of a high-pass filter) can offset the unwrapped "
            "phase by multiples of 360°")
        options.addWidget(self.unwrap_check)
        options.addWidget(QtWidgets.QLabel("Remove delay:"))
        self.delay_box = QtWidgets.QDoubleSpinBox()
        self.delay_box.setRange(-1e5, 1e5)
        self.delay_box.setDecimals(2)
        self.delay_box.setSuffix(" µs")
        self.delay_box.setToolTip(
            "Remove a pure delay from the phase, e.g. the latency from DAC to ADC (about "
            "two batches, 20 µs, plus the analog front ends)")
        options.addWidget(self.delay_box)
        bode_layout.addLayout(options)
        self.bode_view = GraphicsLayoutWidget()
        self.magnitude_plot = self.bode_view.addPlot(
            row=0, col=0, axisItems={"bottom": FrequencyAxis("bottom")})
        self.phase_plot = self.bode_view.addPlot(
            row=1, col=0, axisItems={"bottom": FrequencyAxis("bottom")})
        self.phase_plot.setXLink(self.magnitude_plot)
        self.magnitude_plot.setLabels(left="Magnitude / dB")
        self.phase_plot.setLabels(left="Phase / °", bottom="Frequency / Hz")
        self.legend = self.magnitude_plot.addLegend(offset=(-10, 10))
        for plot in [self.magnitude_plot, self.phase_plot]:
            self._setup_log_plot(plot)
        bode_layout.addWidget(self.bode_view, 1)
        self.margin_label = QtWidgets.QLabel()
        bode_layout.addWidget(self.margin_label)
        self.tabs.addTab(bode, "Bode")

        # Impulse response.
        ir = QtWidgets.QWidget()
        ir_layout = QtWidgets.QVBoxLayout(ir)
        ir_options = QtWidgets.QHBoxLayout()
        ir_options.addWidget(QtWidgets.QLabel("Channel:"))
        self.ir_channel_box = QtWidgets.QComboBox()
        ir_options.addWidget(self.ir_channel_box)
        ir_options.addStretch()
        ir_layout.addLayout(ir_options)
        self.ir_view = GraphicsLayoutWidget()
        self.ir_plot = self.ir_view.addPlot()
        self.ir_plot.setLabels(left="|h| / dB", bottom="Time after sweep start / ms")
        self.ir_plot.showGrid(True, True, 0.3)
        self.ir_plot.setDownsampling(auto=True, mode="peak")
        self.ir_plot.setClipToView(True)
        ir_layout.addWidget(self.ir_view, 1)
        ir_note = QtWidgets.QLabel(
            "Shaded: impulse response window (green), noise window (grey), harmonic "
            "windows (orange, from the right: 2nd, 3rd, …)")
        ir_note.setStyleSheet("color: gray")
        ir_layout.addWidget(ir_note)
        self.tabs.addTab(ir, "Impulse response")

        # Distortion.
        distortion = QtWidgets.QWidget()
        distortion_layout = QtWidgets.QVBoxLayout(distortion)
        distortion_options = QtWidgets.QHBoxLayout()
        distortion_options.addWidget(QtWidgets.QLabel("Channel:"))
        self.distortion_channel_box = QtWidgets.QComboBox()
        distortion_options.addWidget(self.distortion_channel_box)
        distortion_options.addStretch()
        distortion_layout.addLayout(distortion_options)
        self.distortion_view = GraphicsLayoutWidget()
        self.distortion_plot = self.distortion_view.addPlot(
            axisItems={"bottom": FrequencyAxis("bottom")})
        self.distortion_plot.setLabels(left="Harmonic distortion / dBc",
                                       bottom="Fundamental frequency / Hz")
        distortion_note = QtWidgets.QLabel(
            "Amplitude of the harmonics (at k times the fundamental frequency) relative "
            "to the fundamental, with their estimated noise (shaded). Set the number of "
            "harmonics in the analysis settings.")
        distortion_note.setWordWrap(True)
        distortion_note.setStyleSheet("color: gray")
        self.distortion_plot.addLegend(offset=(-10, 10))
        self._setup_log_plot(self.distortion_plot)
        distortion_layout.addWidget(self.distortion_view, 1)
        distortion_layout.addWidget(distortion_note)
        self.tabs.addTab(distortion, "Distortion")

        # Raw data.
        self.data_view = GraphicsLayoutWidget()
        self.data_plot = self.data_view.addPlot()
        self.data_plot.setLabels(left="Voltage / V", bottom="Time / s")
        self.data_plot.addLegend(offset=(-10, 10))
        self.data_plot.showGrid(True, True, 0.3)
        self.data_plot.setDownsampling(auto=True, mode="peak")
        self.data_plot.setClipToView(True)
        self.tabs.addTab(self.data_view, "Captured data")

        self.quantity_box.currentIndexChanged.connect(self._plot_bode)
        self.noise_check.toggled.connect(self._plot_bode)
        self.unwrap_check.toggled.connect(self._plot_bode)
        self.delay_box.valueChanged.connect(self._plot_bode)
        self.harmonics_box.valueChanged.connect(self._plot_bode)
        self.harmonics_box.valueChanged.connect(self._plot_distortion)
        self.harmonics_box.valueChanged.connect(self._save_parameters)
        self.ir_channel_box.currentIndexChanged.connect(self._plot_impulse_response)
        self.distortion_channel_box.currentIndexChanged.connect(self._plot_distortion)
        self.tabs.currentChanged.connect(self._plot_details)
        return panel

    @staticmethod
    def _setup_log_plot(plot):
        plot.setLogMode(True, False)
        plot.showGrid(True, True, 0.3)
        # With a log axis, there is no point in an SI prefix (see iir.channel_settings).
        plot.getAxis("bottom").enableAutoSIPrefix(False)

    #
    # Sweep parameters.
    #

    def _parameters(self) -> dict:
        return {
            "channel": self.channel_box.currentIndex(),
            "f_start": self.start_box.value(),
            "f_stop": self.stop_box.value(),
            "duration": self.duration_box.value(),
            "amplitude": self.amplitude_box.value(),
            "runs": self.runs_box.value(),
            "auto_window": self.auto_window_box.isChecked(),
            "ir_window": self.window_box.value(),
            "points_per_decade": self.points_box.value(),
            "harmonics": self.harmonics_box.value(),
        }

    def _load_parameters(self):
        settings = QtCore.QSettings()
        values = {}
        for key, default in DEFAULTS.items():
            try:
                values[key] = settings.value(f"transfer_function/{key}", default,
                                             type(default))
            except (TypeError, ValueError):
                values[key] = default
        self.channel_box.setCurrentIndex(values["channel"])
        self.start_box.setValue(values["f_start"])
        # The default (or a value from a target with a higher sample rate) can be above
        # the limit for this sample rate (rounded down to a whole kHz).
        max_stop = math.floor(ess.MAX_STOP_FREQUENCY / self.runner.sample_period / 1e3)
        self.stop_box.setValue(min(values["f_stop"], max_stop * 1e3))
        self.duration_box.setValue(values["duration"])
        self.amplitude_box.setValue(values["amplitude"])
        self.runs_box.setValue(values["runs"])
        self.auto_window_box.setChecked(values["auto_window"])
        self.window_box.setValue(values["ir_window"])
        self.points_box.setValue(values["points_per_decade"])
        self.harmonics_box.setValue(values["harmonics"])

    def _save_parameters(self):
        settings = QtCore.QSettings()
        for key, value in self._parameters().items():
            settings.setValue(f"transfer_function/{key}", value)

    def _design(self) -> ess.Sweep:
        p = self._parameters()
        return ess.Sweep.design(p["f_start"], p["f_stop"], p["duration"], p["amplitude"],
                                self.runner.sample_period)

    def _analysis_settings(self, sweep: ess.Sweep) -> ess.AnalysisSettings:
        if self.auto_window_box.isChecked():
            ir_window = ess.default_ir_window(sweep)
        else:
            ir_window = self.window_box.value() * 1e-3
        return ess.AnalysisSettings(ir_window=ir_window,
                                    points_per_decade=self.points_box.value())

    def _update_sweep_info(self):
        try:
            sweep = self._design()
        except ValueError as e:
            self.sweep_info.setText(f"<span style='color: red'>{e}</span>")
            self.run_button.setEnabled(False)
            return
        ir_window = self._analysis_settings(sweep).ir_window
        if self.auto_window_box.isChecked():
            self.window_box.blockSignals(True)
            self.window_box.setValue(ir_window * 1e3)
            self.window_box.blockSignals(False)
        self.window_box.setEnabled(not self.auto_window_box.isChecked())

        capture = PRE_TRIGGER + post_trigger_duration(sweep, ir_window)
        lines = [
            f"{format_frequency(sweep.f_start)} to {format_frequency(sweep.f_stop)} "
            f"in {sweep.duration:.3g} s",
            f"Capture: {capture:.3g} s per sweep; 2nd harmonic "
            f"{sweep.harmonic_delay(2) * sweep.sample_period * 1e3:.3g} ms ahead",
        ]
        if sweep.f_start > 1.05 * self.start_box.value():
            lines.append(
                "<span style='color: darkorange'>Start frequency raised; increase "
                "the duration to reach lower frequencies</span>")
        self.sweep_info.setText("<br>".join(lines))
        self._update_buttons()

    #
    # Measurements.
    #

    def _update_buttons(self):
        running = self._task is not None
        selected = self._current() is not None
        try:
            self._design()
            valid = True
        except ValueError:
            valid = False
        self.run_button.setEnabled(not running and valid)
        self.cancel_button.setEnabled(running)
        self.reanalyse_button.setEnabled(not running and selected)
        for button in [self.save_button, self.csv_button, self.remove_button]:
            button.setEnabled(selected and not running)

    def _progress(self, message: str, fraction: float):
        self.status_label.setText(message)
        self.progress_bar.setValue(int(1000 * fraction))

    def _run(self):
        try:
            sweep = self._design()
        except ValueError as e:
            self._progress(str(e), 0)
            return
        self._save_parameters()
        self._task = asyncio.ensure_future(
            self._measure(sweep, self.channel_box.currentIndex(), self.runs_box.value(),
                          self._analysis_settings(sweep)))
        self._update_buttons()

    def _cancel(self):
        if self._task is not None:
            self._task.cancel()

    async def _measure(self, sweep: ess.Sweep, channel: int, runs: int,
                       settings: ess.AnalysisSettings):
        try:
            measurement = await self.runner.run(sweep, channel, runs, settings.ir_window,
                                                self._progress)
            self._progress("Analysing…", 1)
            await asyncio.get_running_loop().run_in_executor(None, measurement.analyse,
                                                             settings)
            self._add(measurement)
            warnings = "; ".join(measurement.analysis.warnings)
            self._progress(f"Done. {warnings}" if warnings else "Done", 1)
        except asyncio.CancelledError:
            self._progress("Cancelled", 0)
        except Exception as e:
            logger.exception("Transfer function measurement failed")
            self._progress(f"<span style='color: red'>Failed: {e}</span>", 0)
        finally:
            self._task = None
            self._update_buttons()

    def _add(self, measurement: Measurement):
        self.measurements.append(measurement)
        self._colours[id(measurement)] = COLOURS[self._next_colour % len(COLOURS)]
        self._next_colour += 1
        item = QtWidgets.QListWidgetItem(measurement.label)
        item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                      | QtCore.Qt.ItemFlag.ItemIsEditable)
        item.setCheckState(QtCore.Qt.CheckState.Checked)
        item.setForeground(pg.mkColor(self._colours[id(measurement)]))
        self.list_widget.blockSignals(True)
        self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)
        self.list_widget.setCurrentRow(len(self.measurements) - 1)
        self._plot_bode()

    def _current(self) -> Measurement | None:
        row = self.list_widget.currentRow()
        return self.measurements[row] if 0 <= row < len(self.measurements) else None

    def _item_changed(self, item):
        measurement = self.measurements[self.list_widget.row(item)]
        if item.text() != measurement.label:
            measurement.name = item.text()
        self._plot_bode()

    def _selection_changed(self, _row):
        measurement = self._current()
        for box in [self.ir_channel_box, self.distortion_channel_box]:
            previous = box.currentText()
            box.blockSignals(True)
            box.clear()
            if measurement is not None:
                box.addItems(measurement.response_names)
                default = (previous if previous in measurement.response_names else
                           f"ADC{measurement.channel}")
                box.setCurrentText(default)
            box.blockSignals(False)
        self._data_cache = None
        self._update_buttons()
        self._plot_details()

    def _remove(self):
        row = self.list_widget.currentRow()
        if row < 0:
            return
        measurement = self.measurements.pop(row)
        self._colours.pop(id(measurement), None)
        self.list_widget.takeItem(row)
        self._plot_bode()
        self._selection_changed(self.list_widget.currentRow())

    def _reanalyse(self):
        measurement = self._current()
        if measurement is None:
            return
        settings = self._analysis_settings(measurement.sweep)

        async def reanalyse():
            try:
                self._progress("Analysing…", 1)
                await asyncio.get_running_loop().run_in_executor(
                    None, measurement.analyse, settings)
                warnings = "; ".join(measurement.analysis.warnings)
                self._progress(f"Done. {warnings}" if warnings else "Done", 1)
            except Exception as e:
                logger.exception("Analysis failed")
                self._progress(f"<span style='color: red'>Failed: {e}</span>", 0)
            finally:
                self._task = None
                self._update_buttons()
                self._plot_bode()
                self._plot_details()

        self._task = asyncio.ensure_future(reanalyse())
        self._update_buttons()

    #
    # Files.
    #

    def _default_name(self, measurement: Measurement, extension: str) -> str:
        name = f"tf_{measurement.device}_{measurement.timestamp}_ch{measurement.channel}"
        return re.sub(r"[^\w\-.]", "_", name) + extension

    def _directory(self) -> str:
        return QtCore.QSettings().value("transfer_function/directory", os.getcwd(), str)

    def _set_directory(self, path: str):
        QtCore.QSettings().setValue("transfer_function/directory", os.path.dirname(path))

    def _load(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Load measurements",
                                                          self._directory(),
                                                          "HDF5 files (*.h5 *.hdf5)")
        if not paths:
            return
        self._set_directory(paths[0])

        async def load():
            loop = asyncio.get_running_loop()
            for path in paths:
                try:
                    measurement = await loop.run_in_executor(None, Measurement.load, path)
                    if measurement.analysis is None:
                        settings = self._analysis_settings(measurement.sweep)
                        await loop.run_in_executor(None, measurement.analyse, settings)
                    if not measurement.name:
                        measurement.name = os.path.splitext(os.path.basename(path))[0]
                    self._add(measurement)
                except Exception as e:
                    logger.exception("Failed to load %s", path)
                    QtWidgets.QMessageBox.warning(self, "Load failed",
                                                  f"Failed to load {path}:\n{e}")

        asyncio.ensure_future(load())

    def _save(self):
        measurement = self._current()
        if measurement is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save measurement",
            os.path.join(self._directory(), self._default_name(measurement, ".h5")),
            "HDF5 files (*.h5)")
        if not path:
            return
        self._set_directory(path)

        async def save():
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, measurement.save, path)
                self._progress(f"Saved to {path}", self.progress_bar.value() / 1000)
            except Exception as e:
                logger.exception("Failed to save %s", path)
                QtWidgets.QMessageBox.warning(self, "Save failed",
                                              f"Failed to save {path}:\n{e}")

        asyncio.ensure_future(save())

    def _export_csv(self):
        measurement = self._current()
        if measurement is None or measurement.analysis is None:
            return
        quantity = self.quantity_box.currentData()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export CSV",
            os.path.join(self._directory(),
                         self._default_name(measurement, f"_{quantity}.csv")),
            "CSV files (*.csv)")
        if not path:
            return
        self._set_directory(path)
        try:
            measurement.export_csv(path, quantity)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(e))

    def _export_plot(self):
        views = {
            0: self.bode_view,
            1: self.ir_view,
            2: self.distortion_view,
            3: self.data_view
        }
        view = views[self.tabs.currentIndex()]
        path, chosen = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export plot", self._directory(),
            "PNG image (*.png);;SVG image (*.svg)")
        if not path:
            return
        self._set_directory(path)
        svg = path.lower().endswith(".svg") or (chosen.startswith("SVG")
                                                and not path.lower().endswith(".png"))
        try:
            exporter = (pg.exporters.SVGExporter(view.scene())
                        if svg else pg.exporters.ImageExporter(view.scene()))
            exporter.export(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(e))

    #
    # Plots.
    #

    def _checked(self) -> list[Measurement]:
        return [
            m for i, m in enumerate(self.measurements)
            if self.list_widget.item(i).checkState() == QtCore.Qt.CheckState.Checked
            and m.analysis is not None
        ]

    def _plot_bode(self):
        quantity = self.quantity_box.currentData()
        self.magnitude_plot.clear()
        self.phase_plot.clear()
        self.legend.clear()
        delay = self.delay_box.value() * 1e-6
        unwrap = self.unwrap_check.isChecked()
        margins = []
        for measurement in self._checked():
            f = measurement.analysis.frequencies
            value, noise = measurement.quantity(quantity)
            value = value * np.exp(2j * np.pi * f * delay)
            colour = self._colours[id(measurement)]
            pen = pg.mkPen(colour, width=1.5)
            curve = self.magnitude_plot.plot(f,
                                             _db(value),
                                             pen=pen,
                                             connect="finite",
                                             name=measurement.label)
            curve.setToolTip(measurement.label)
            if self.noise_check.isChecked() and np.any(np.isfinite(noise)):
                self.magnitude_plot.addItem(_NoiseShading(f, _db(noise), colour))
            self.phase_plot.plot(f, _phase(value, unwrap), pen=pen, connect="finite")

            for k in self._shown_harmonics(measurement):
                harmonic_f, harmonic, harmonic_noise = measurement.harmonic(quantity, k)
                magnitude = np.abs(harmonic)
                with np.errstate(invalid="ignore"):
                    magnitude[magnitude < HARMONIC_THRESHOLD * harmonic_noise] = np.nan
                style = HARMONIC_STYLES.get(k, QtCore.Qt.PenStyle.DotLine)
                harmonic_pen = (pg.mkPen(colour, width=1, dash=style) if isinstance(
                    style, list) else pg.mkPen(colour, width=1, style=style))
                self.magnitude_plot.plot(harmonic_f,
                                         _db(magnitude),
                                         pen=harmonic_pen,
                                         connect="finite",
                                         name=f"{measurement.label}: H{k}")

            designed = self._designed_controller(measurement, f)
            if quantity == "controller" and designed is not None:
                designed_pen = pg.mkPen(colour, width=1, style=QtCore.Qt.PenStyle.DotLine)
                self.magnitude_plot.plot(f,
                                         _db(designed),
                                         pen=designed_pen,
                                         name=f"{measurement.label} (designed)")
                self.phase_plot.plot(f, _phase(designed, unwrap), pen=designed_pen)
            if quantity == "loop":
                crossover = _crossover(f, value)
                if crossover is not None:
                    margins.append(
                        f"<span style='color: {colour}'>{measurement.label}: unity gain "
                        f"at {format_frequency(crossover[0])}, phase margin "
                        f"{crossover[1]:.1f}°</span>")
        self.margin_label.setText("<br>".join(margins))
        self.margin_label.setVisible(bool(margins))

    def _designed_controller(self, measurement: Measurement, f: np.ndarray):
        """The response of the filter configured during the measurement, if known (in
        V/V, i.e. including the AFE gain)."""
        n = measurement.channel
        ba = measurement.settings.get("biquads", {}).get(str(n))
        gain = measurement.settings.get("afe_gains", {}).get(str(n))
        running = measurement.settings.get(f"settings/ch/{n}/run", "Run") == "Run"
        if ba is None or gain is None or not running:
            return None
        # idsp convention: y0 = b0 x0 + b1 x1 + b2 x2 + a1 y1 + a2 y2.
        _, h = signal.freqz(ba[:3], [1, -ba[3], -ba[4]],
                            worN=f,
                            fs=1 / measurement.sweep.sample_period)
        return gain * h

    def _plot_details(self, *_):
        tab = self.tabs.currentIndex()
        if tab == 1:
            self._plot_impulse_response()
        elif tab == 2:
            self._plot_distortion()
        elif tab == 3:
            self._plot_data()

    def _plot_impulse_response(self, *_):
        self.ir_plot.clear()
        measurement = self._current()
        channel = self.ir_channel_box.currentIndex()
        if measurement is None or measurement.analysis is None or channel < 0:
            return
        analysis = measurement.analysis
        regions = [(analysis.ir_window, (0, 160, 0, 50)),
                   (analysis.noise_window, (128, 128, 128, 50))]
        regions += [(window, (255, 140, 0, 50))
                    for window in analysis.harmonic_windows.values()]
        for span, brush in regions:
            if span is not None:
                region = pg.LinearRegionItem([1e3 * span[0], 1e3 * span[1]],
                                             movable=False,
                                             brush=pg.mkBrush(*brush))
                self.ir_plot.addItem(region)
        h = analysis.impulse_responses[channel]
        floor = np.max(np.abs(h)) * 1e-9 + 1e-30
        self.ir_plot.plot(1e3 * analysis.ir_time,
                          _db(np.abs(h) + floor),
                          pen=pg.mkPen(self._colours[id(measurement)]))

    def _shown_harmonics(self, measurement: Measurement) -> list[int]:
        return [
            k for k in sorted(measurement.analysis.harmonics)
            if k <= 1 + self.harmonics_box.value()
        ]

    def _plot_distortion(self, *_):
        self.distortion_plot.clear()
        self.distortion_plot.legend.clear()
        measurement = self._current()
        channel = self.distortion_channel_box.currentIndex()
        if measurement is None or measurement.analysis is None or channel < 0:
            return
        analysis = measurement.analysis
        log_f = np.log(analysis.frequencies)
        fundamental = np.abs(analysis.responses[channel])
        for i, k in enumerate(self._shown_harmonics(measurement)):
            f = analysis.harmonic_frequencies[k]
            reference = np.interp(np.log(f), log_f, fundamental)
            colour = COLOURS[i % len(COLOURS)]
            self.distortion_plot.plot(
                f,
                _db(np.abs(analysis.harmonics[k][channel]) / reference),
                pen=pg.mkPen(colour, width=1.5),
                connect="finite",
                name=f"Harmonic {k}")
            noise = analysis.harmonic_noise[k][channel]
            if np.any(np.isfinite(noise)):
                self.distortion_plot.addItem(
                    _NoiseShading(f, _db(noise / reference), colour))

    def _plot_data(self):
        self.data_plot.clear()
        self.data_plot.legend.clear()
        measurement = self._current()
        if measurement is None:
            return
        if self._data_cache is None or self._data_cache[0] is not measurement:
            # All samples, as the plot downsamples to the visible range itself. Lost
            # batches are left out, as interpolating them over the length of a gap
            # would show a straight line.
            data = measurement.volts(0, interpolate=False)
            t = np.arange(data.shape[1]) * measurement.sweep.sample_period
            self._data_cache = (measurement, t, data)
        _, t, data = self._data_cache
        for i, (name, trace) in enumerate(zip(measurement.channel_names, data)):
            self.data_plot.plot(t,
                                trace,
                                pen=pg.mkPen(COLOURS[i % len(COLOURS)]),
                                name=name,
                                connect="finite")


class TransferFunctionMixin:
    """Adds the transfer function window to the main window of a `dual-iir`-like target.

    Expects the `channels` of the window to be `AbstractChannelSettings`.
    """

    def _add_transfer_function_action(self):
        """Add the (initially disabled) action to the Tools menu."""
        self._sweep_runner = None
        self._transfer_function_window = None
        self.transferFunctionAction = self.tools_menu().addAction("&Transfer function…")
        self.transferFunctionAction.setShortcut("Ctrl+T")
        self.transferFunctionAction.setEnabled(False)
        self.transferFunctionAction.triggered.connect(self.show_transfer_function)

    def set_sweep_runner(self, runner: SweepRunner):
        """Enable transfer function measurements using the given `SweepRunner`."""
        self._sweep_runner = runner
        self.transferFunctionAction.setEnabled(True)

    def set_firmware(self, firmware):
        super().set_firmware(firmware)
        # The measurements need the swept-sine source.
        available = firmware.has("settings/trigger")
        action = self.transferFunctionAction
        action.setEnabled(available and self._sweep_runner is not None)
        unavailable = f"Transfer function (not available in firmware {firmware})"
        action.setText("&Transfer function…" if available else unavailable)

    def show_transfer_function(self):
        if self._transfer_function_window is None:
            self._transfer_function_window = TransferFunctionWindow(
                self._sweep_runner, self)
        self._transfer_function_window.show()
        self._transfer_function_window.raise_()
        self._transfer_function_window.activateWindow()

    def afe_gains(self) -> list[int]:
        return [int(channel.afeGainBox.currentText()[1:]) for channel in self.channels]

    def settings_snapshot(self) -> dict:
        """The current settings (by topic), the AFE gains, and the coefficients of the
        (first) biquad of each channel, to store with measurements."""
        settings = super().settings_snapshot()
        gains = self.afe_gains()
        settings["afe_gains"] = {str(ch): gain for ch, gain in enumerate(gains)}
        settings["biquads"] = {
            str(ch): channel.iir_widgets[0].coefficients
            for ch, channel in enumerate(self.channels)
        }
        return settings
