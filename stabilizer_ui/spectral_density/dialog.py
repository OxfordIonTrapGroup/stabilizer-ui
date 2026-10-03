"""Window showing long-term estimates of the power spectral density of the stream data."""

from __future__ import annotations

import datetime
import logging
import os
import re

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets
from stabilizer_psd import FFT_SIZE

from .estimate import DETREND_METHODS, Estimate, Spectrum, load_csv, save_csv
from ..plot import (COLOURS, FrequencyAxis, GraphicsLayoutWidget, LogAxis,
                    format_frequency)
from ..utils import format_duration

logger = logging.getLogger(__name__)

#: Settings remembered between sessions (QSettings key, default).
DEFAULTS = {
    "max_averages": 1000,
    "min_averages": 1,
    "detrend": "mean",
    "cumulative": False,
}

#: Interval between plot updates, in milliseconds.
UPDATE_INTERVAL = 250


class _Trace:
    """A curve of the plot: the live estimate of a source of the stream (`source` is its
    index), or a stored spectrum."""

    def __init__(self,
                 name: str,
                 unit: str,
                 colour: str,
                 source: int | None = None,
                 spectrum: Spectrum | None = None):
        self.name = name
        self.unit = unit
        self.colour = colour
        self.source = source
        self.spectrum = spectrum
        self.curve: pg.PlotDataItem | None = None
        self.rms_curve: pg.PlotDataItem | None = None

    @property
    def live(self) -> bool:
        return self.source is not None


class SpectralDensityWindow(QtWidgets.QDialog):
    """Estimates the power spectral density of each source of the stream while open, and
    shows it with stored spectra for comparison."""

    def __init__(self, stream_thread, device: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Spectral Density [{device}]")
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint)
        self.stream_thread = stream_thread
        self.device = device
        parser = stream_thread.parser
        self._names = list(parser.StreamData._fields)
        self._units = parser.units()
        self._traces = [
            _Trace(name, unit, COLOURS[i % len(COLOURS)], source=i)
            for i, (name, unit) in enumerate(zip(self._names, self._units))
        ]
        self._next_colour = len(self._traces)
        self._stream_message: str | None = None
        #: The stages of the latest estimate.
        self._stages = []

        layout = QtWidgets.QHBoxLayout(self)
        splitter = QtWidgets.QSplitter()
        layout.addWidget(splitter)
        controls = QtWidgets.QScrollArea()
        controls.setWidget(self._make_controls())
        controls.setWidgetResizable(True)
        controls.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        controls.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        splitter.addWidget(controls)
        splitter.addWidget(self._make_plot())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 900])
        self.resize(1200, 700)
        for button in self.findChildren(QtWidgets.QPushButton):
            button.setAutoDefault(False)

        self._load_parameters()
        self._estimate = self._new_estimate()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(UPDATE_INTERVAL)
        self._timer.timeout.connect(self._update)

        for trace in self._traces:
            self._add_item(trace)
        self._rebuild_plot()
        self._update_buttons()
        self._update_status([])

    #
    # Layout.
    #

    def _make_controls(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        group = QtWidgets.QGroupBox("Averaging")
        form = QtWidgets.QFormLayout(group)
        self.max_averages_box = QtWidgets.QSpinBox()
        self.max_averages_box.setRange(1, 10**6)
        # Reducing the limit discards the averages beyond it, also while typing.
        self.max_averages_box.setKeyboardTracking(False)
        self.max_averages_box.setToolTip(
            "Each stage averages up to this many spectra, and then continues with an "
            "exponentially weighted average, following changes with this time constant "
            "(in spectra of the stage). The stages for lower frequencies take longer, so "
            "after a change of the signal, the spectrum can show steps between stages "
            "for a while (reset to start again).")
        form.addRow("Max. averages:", self.max_averages_box)
        self.min_averages_box = QtWidgets.QSpinBox()
        self.min_averages_box.setRange(1, 10**6)
        self.min_averages_box.setKeyboardTracking(False)
        self.min_averages_box.setToolTip(
            "The lower frequencies are only shown once their stage has averaged this "
            "many spectra; the first ones are noisy")
        form.addRow("Min. averages:", self.min_averages_box)
        self.detrend_box = QtWidgets.QComboBox()
        for value, label in DETREND_METHODS:
            self.detrend_box.addItem(label, value)
        self.detrend_box.setToolTip(
            "Offset removed from each segment before the FFT: the mean, the value at "
            "the midpoint, a line between the first and last value, or none. Changes "
            "apply to the following segments.")
        form.addRow("Detrend:", self.detrend_box)
        buttons = QtWidgets.QHBoxLayout()
        self.pause_button = QtWidgets.QPushButton("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip("Stop averaging (it also stops while the window is "
                                     "closed)")
        self.reset_button = QtWidgets.QPushButton("Reset")
        self.reset_button.setToolTip("Discard the averages and start again")
        buttons.addWidget(self.pause_button)
        buttons.addWidget(self.reset_button)
        form.addRow(buttons)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setWordWrap(True)
        form.addRow(self.status_label)
        hint = QtWidgets.QLabel(
            "The spectrum of each signal is estimated in stages, each decimating the "
            "signal by 8 for the next, so that it extends to lower frequencies the "
            f"longer it runs (with {FFT_SIZE}-point FFTs, Hann window).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        form.addRow(hint)
        layout.addWidget(group)

        group = QtWidgets.QGroupBox("Traces")
        group_layout = QtWidgets.QVBoxLayout(group)
        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setToolTip(
            "Checked traces are shown. Stored traces can be renamed (double-click).")
        group_layout.addWidget(self.list_widget)
        buttons = QtWidgets.QGridLayout()
        self.store_button = QtWidgets.QPushButton("Store")
        self.store_button.setToolTip("Keep the current estimates of the checked signals "
                                     "for comparison")
        self.remove_button = QtWidgets.QPushButton("Remove")
        self.load_button = QtWidgets.QPushButton("Load…")
        self.save_button = QtWidgets.QPushButton("Save…")
        self.save_button.setToolTip("Save the checked traces as CSV")
        traces_buttons = [
            self.store_button, self.remove_button, self.load_button, self.save_button
        ]
        for i, button in enumerate(traces_buttons):
            buttons.addWidget(button, i // 2, i % 2)
        group_layout.addLayout(buttons)
        layout.addWidget(group, 1)

        self.max_averages_box.valueChanged.connect(self._max_averages_changed)
        self.min_averages_box.valueChanged.connect(self._parameters_changed)
        self.detrend_box.currentIndexChanged.connect(self._detrend_changed)
        self.pause_button.toggled.connect(self._pause_toggled)
        self.reset_button.clicked.connect(self._reset)
        self.store_button.clicked.connect(self._store)
        self.remove_button.clicked.connect(self._remove)
        self.load_button.clicked.connect(self._load)
        self.save_button.clicked.connect(self._save)
        self.list_widget.itemChanged.connect(self._item_changed)
        self.list_widget.currentRowChanged.connect(lambda _: self._update_buttons())
        # Delete (or Backspace, labelled delete on Mac keyboards) in the list removes the
        # selected trace, unless it is being renamed.
        for key in [QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace]:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(key), self.list_widget)
            shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self.remove_button.click)
        return panel

    def _make_plot(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        options = QtWidgets.QHBoxLayout()
        self.cumulative_check = QtWidgets.QCheckBox("Cumulative RMS")
        self.cumulative_check.setToolTip(
            "Show the RMS of each signal above each frequency, i.e. the power spectral "
            "density integrated from the highest frequency down")
        options.addWidget(self.cumulative_check)
        options.addStretch()
        layout.addLayout(options)
        self.view = GraphicsLayoutWidget()
        self.plot = self.view.addPlot(row=0,
                                      col=0,
                                      axisItems={
                                          "bottom": FrequencyAxis("bottom"),
                                          "left": LogAxis("left")
                                      })
        self.legend = self.plot.addLegend(offset=(-10, 10))
        # Below, while shown (see `_rebuild_plot()`).
        self.rms_plot = pg.PlotItem(axisItems={
            "bottom": FrequencyAxis("bottom"),
            "left": LogAxis("left")
        })
        self.rms_plot.setXLink(self.plot)
        for plot in [self.plot, self.rms_plot]:
            plot.setLogMode(True, True)
            plot.showGrid(True, True, 0.3)
            # With a log axis, there is no point in an SI prefix.
            for axis in ["bottom", "left"]:
                plot.getAxis(axis).enableAutoSIPrefix(False)
        layout.addWidget(self.view, 1)
        self.cumulative_check.toggled.connect(self._parameters_changed)
        self.cumulative_check.toggled.connect(self._rebuild_plot)
        return panel

    #
    # Parameters.
    #

    def _parameters(self) -> dict:
        return {
            "max_averages": self.max_averages_box.value(),
            "min_averages": self.min_averages_box.value(),
            "detrend": self.detrend_box.currentData(),
            "cumulative": self.cumulative_check.isChecked(),
        }

    def _load_parameters(self):
        settings = QtCore.QSettings()
        values = {}
        for key, default in DEFAULTS.items():
            try:
                values[key] = settings.value(f"spectral_density/{key}", default,
                                             type(default))
            except (TypeError, ValueError):
                values[key] = default
        for box in [
                self.max_averages_box, self.min_averages_box, self.detrend_box,
                self.cumulative_check
        ]:
            box.blockSignals(True)
        self.max_averages_box.setValue(values["max_averages"])
        self.min_averages_box.setMaximum(self.max_averages_box.value())
        self.min_averages_box.setValue(values["min_averages"])
        index = self.detrend_box.findData(values["detrend"])
        self.detrend_box.setCurrentIndex(max(index, 0))
        self.cumulative_check.setChecked(values["cumulative"])
        for box in [
                self.max_averages_box, self.min_averages_box, self.detrend_box,
                self.cumulative_check
        ]:
            box.blockSignals(False)

    def _parameters_changed(self):
        settings = QtCore.QSettings()
        for key, value in self._parameters().items():
            settings.setValue(f"spectral_density/{key}", value)

    def _max_averages_changed(self, value: int):
        self._estimate.set_max_averages(value)
        # A stage never has more averages than the limit.
        self.min_averages_box.setMaximum(value)
        self._parameters_changed()

    def _detrend_changed(self):
        self._estimate.set_detrend(self.detrend_box.currentData())
        self._parameters_changed()

    #
    # Estimation.
    #

    def _new_estimate(self) -> Estimate:
        return Estimate(self._names, self._units, self.stream_thread.sample_period,
                        self.max_averages_box.value(), self.detrend_box.currentData())

    def _running(self) -> bool:
        return self.isVisible() and not self.pause_button.isChecked()

    def _connect(self):
        """Feed the estimate with the stream data while running."""
        running = self._running()
        self.stream_thread.set_consumer(self._estimate.process if running else None)
        if running:
            self._timer.start()
        else:
            self._timer.stop()
        self._update_status(None)

    def showEvent(self, event):
        super().showEvent(event)
        self._connect()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._connect()

    def _pause_toggled(self, paused: bool):
        self.pause_button.setText("Resume" if paused else "Pause")
        self._connect()
        if not paused:
            self._update()

    def _reset(self):
        self._estimate = self._new_estimate()
        self._connect()
        for trace in self._traces:
            if trace.live:
                trace.spectrum = None
                self._set_curve_data(trace)
        self._update_buttons()
        self._update_status([])

    def set_stream_status(self, message: str | None):
        """Show why the stream is not directed to this client (`None` if it is)."""
        self._stream_message = message
        self._update_status(None)

    def _update(self):
        timestamp = datetime.datetime.now().isoformat(timespec="seconds")
        spectra, stages = self._estimate.spectra(self.min_averages_box.value(), timestamp)
        live = [trace for trace in self._traces if trace.live]
        had_data = any(trace.spectrum is not None for trace in live)
        for trace in live:
            trace.spectrum = spectra[trace.source]
            self._set_curve_data(trace)
        if not had_data:
            self._update_buttons()
        self._update_status(stages)

    def _update_status(self, stages):
        """Show the state of the estimate, and of its stages (if not `None`)."""
        estimate = self._estimate
        if self._stream_message is not None:
            state = (f"<span style='color: darkorange'>No stream: "
                     f"{self._stream_message}</span>")
        elif self.pause_button.isChecked():
            state = "Paused"
        else:
            state = "Averaging"
        lines = [f"{state}; {format_duration(estimate.duration)} of data"]
        if estimate.lost:
            fraction = estimate.lost / (estimate.samples + estimate.lost)
            lines.append(f"{100 * fraction:.3g} % of the data lost (left out)")
        if stages is None:
            stages = self._stages
        self._stages = stages
        period = estimate.sample_period
        included = [stage for stage in stages if stage.included]
        if included:
            lowest, highest = included[0], included[-1]
            lines.append(f"Resolution: {format_frequency(lowest.bin_width / period)} "
                         f"({lowest.count} averages) to "
                         f"{format_frequency(highest.bin_width / period)} "
                         f"({highest.count})")
        if stages and not stages[0].included:
            # The stage which extends the spectrum to lower frequencies next.
            stage = stages[0]
            resolution = format_frequency(stage.bin_width / period)
            if stage.count == 0:
                remaining = (FFT_SIZE - stage.pending) * stage.decimation * period
                lines.append(f"Next: {resolution} in {format_duration(remaining)}")
            else:
                lines.append(f"Next: {resolution} after {self.min_averages_box.value()} "
                             f"averages ({stage.count} so far)")
        self.status_label.setText("<br>".join(lines))

    #
    # Traces.
    #

    def _add_item(self, trace: _Trace):
        item = QtWidgets.QListWidgetItem(trace.name)
        flags = item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable
        if not trace.live:
            flags |= QtCore.Qt.ItemFlag.ItemIsEditable
        else:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        item.setFlags(flags)
        item.setCheckState(QtCore.Qt.CheckState.Checked)
        item.setForeground(pg.mkColor(trace.colour))
        self.list_widget.blockSignals(True)
        self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)

    def _add_stored(self, spectrum: Spectrum):
        colour = COLOURS[self._next_colour % len(COLOURS)]
        self._next_colour += 1
        trace = _Trace(spectrum.name, spectrum.unit, colour, spectrum=spectrum)
        self._traces.append(trace)
        self._add_item(trace)

    def _checked(self) -> list[_Trace]:
        return [
            trace for i, trace in enumerate(self._traces)
            if self.list_widget.item(i).checkState() == QtCore.Qt.CheckState.Checked
        ]

    def _item_changed(self, item):
        trace = self._traces[self.list_widget.row(item)]
        if not trace.live and item.text() != trace.name:
            trace.name = item.text()
            trace.spectrum.name = item.text()
        self._rebuild_plot()
        self._update_buttons()

    def _update_buttons(self):
        row = self.list_widget.currentRow()
        checked = [trace for trace in self._checked() if trace.spectrum is not None]
        self.store_button.setEnabled(any(trace.live for trace in checked))
        self.save_button.setEnabled(bool(checked))
        self.remove_button.setEnabled(0 <= row < len(self._traces)
                                      and not self._traces[row].live)

    def _store(self):
        time = datetime.datetime.now().strftime("%H:%M:%S")
        for trace in self._checked():
            if trace.live and trace.spectrum is not None:
                spectrum = trace.spectrum
                self._add_stored(
                    Spectrum(f"{trace.name} {time}", spectrum.unit, spectrum.frequencies,
                             spectrum.asd, spectrum.duration, spectrum.timestamp))
        self._rebuild_plot()
        self._update_buttons()

    def _remove(self):
        row = self.list_widget.currentRow()
        if not (0 <= row < len(self._traces)) or self._traces[row].live:
            return
        del self._traces[row]
        self.list_widget.takeItem(row)
        self._rebuild_plot()
        self._update_buttons()

    #
    # Files.
    #

    def _directory(self) -> str:
        return QtCore.QSettings().value("spectral_density/directory", os.getcwd(), str)

    def _set_directory(self, path: str):
        QtCore.QSettings().setValue("spectral_density/directory", os.path.dirname(path))

    def _load(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Load spectra",
                                                          self._directory(),
                                                          "CSV files (*.csv)")
        if not paths:
            return
        self._set_directory(paths[0])
        for path in paths:
            file_name = os.path.splitext(os.path.basename(path))[0]
            try:
                for spectrum in load_csv(path):
                    spectrum.name = f"{spectrum.name} ({file_name})"
                    self._add_stored(spectrum)
            except Exception as e:
                logger.exception("Failed to load %s", path)
                QtWidgets.QMessageBox.warning(self, "Load failed",
                                              f"Failed to load {path}:\n{e}")
        self._rebuild_plot()
        self._update_buttons()

    def _save(self):
        spectra = [trace.spectrum for trace in self._checked() if trace.spectrum]
        if not spectra:
            return
        timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H_%M_%S")
        name = re.sub(r"[^\w\-.]", "_", f"psd_{self.device}_{timestamp}") + ".csv"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save spectra", os.path.join(self._directory(), name),
            "CSV files (*.csv)")
        if not path:
            return
        self._set_directory(path)
        try:
            save_csv(path, spectra)
        except Exception as e:
            logger.exception("Failed to save %s", path)
            QtWidgets.QMessageBox.warning(self, "Save failed",
                                          f"Failed to save {path}:\n{e}")

    #
    # Plot.
    #

    def _rebuild_plot(self):
        """Create the curves of the checked traces."""
        self.plot.clear()
        self.rms_plot.clear()
        self.legend.clear()
        checked = self._checked()
        cumulative = self.cumulative_check.isChecked()
        for trace in self._traces:
            trace.curve = trace.rms_curve = None
            if trace not in checked:
                continue
            pen = pg.mkPen(trace.colour, width=1.5 if trace.live else 1)
            trace.curve = self.plot.plot(name=trace.name, pen=pen, connect="finite")
            if cumulative:
                trace.rms_curve = self.rms_plot.plot(pen=pen, connect="finite")
            self._set_curve_data(trace)

        units = sorted({trace.unit for trace in checked})
        asd_units = ", ".join(f"{unit}/√Hz" for unit in units)
        rms_units = ", ".join(units)
        if len(units) > 1:
            rms_units = f"({rms_units})"
        self.plot.setLabels(left=f"ASD / ({asd_units})" if units else "ASD")
        self.rms_plot.setLabels(
            left=f"Cumulative RMS / {rms_units}" if units else "Cumulative RMS",
            bottom="Frequency / Hz")
        shown = self.rms_plot in self.view.ci.items
        if cumulative and not shown:
            self.view.addItem(self.rms_plot, row=1, col=0)
            self.view.ci.layout.setRowStretchFactor(0, 2)
        elif not cumulative and shown:
            self.view.removeItem(self.rms_plot)
        self.plot.setLabel("bottom", None if cumulative else "Frequency / Hz")

    def _set_curve_data(self, trace: _Trace):
        spectrum = trace.spectrum
        curves = [(trace.curve, lambda: spectrum.asd),
                  (trace.rms_curve, lambda: spectrum.cumulative_rms())]
        for curve, values in curves:
            if curve is None:
                continue
            if spectrum is None:
                curve.setData([], [])
                continue
            values = values()
            # Leave out zeros (e.g. the cumulative RMS at the highest frequency), as the
            # axis is logarithmic.
            curve.setData(spectrum.frequencies, np.where(values > 0, values, np.nan))
