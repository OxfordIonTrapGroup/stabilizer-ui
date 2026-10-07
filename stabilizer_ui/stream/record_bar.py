"""Controls below the scope for recording the stream data to a file."""

from __future__ import annotations

import datetime
import html
import logging
import os
import re
import time
from typing import Callable

from PyQt6 import QtCore, QtGui, QtWidgets
from stabilizer.stream_parser import Parser

from .decimation import MAX_DEPTH, PASSBAND, available as decimation_available
from .recorder import StreamRecorder
from ..utils import format_duration, format_frequency, format_size

logger = logging.getLogger(__name__)

#: Interval between updates of the status while recording, in milliseconds.
UPDATE_INTERVAL = 250
#: Time without stream data after which the status mentions it, in seconds.
NO_DATA_WARNING = 1.0
#: The units offered for the time limit, and their length in seconds.
TIME_UNITS = (("s", 1), ("min", 60), ("h", 3600))


def _record_icon() -> QtGui.QIcon:
    """A red dot."""
    size, ratio = 12, 2
    pixmap = QtGui.QPixmap(size * ratio, size * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor("red"))
    painter.drawEllipse(QtCore.QRectF(1, 1, size - 2, size - 2))
    painter.end()
    return QtGui.QIcon(pixmap)


def _warning(text: str) -> str:
    return f"<span style='color: darkorange'>{text}</span>"


class RecordDialog(QtWidgets.QDialog):
    """Asks which sources of the stream to record, at which sample rate (the full rate of
    the stream, or lower, decimated), and for how long (until stopped, or up to a time
    limit), showing the data rate of the selection.

    The choice is remembered (in the `QSettings`) when the dialog is accepted, and shown
    again the next time it is opened (`load()`).
    """

    def __init__(self, parser: Parser, sample_period: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Record stream")
        self._names = parser.StreamData._fields
        self._sample_period = sample_period
        layout = QtWidgets.QFormLayout(self)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetFixedSize)

        sources = QtWidgets.QHBoxLayout()
        self.source_boxes = []
        for name in self._names:
            box = QtWidgets.QCheckBox(name)
            box.toggled.connect(self._update)
            sources.addWidget(box)
            self.source_boxes.append(box)
        sources.addStretch()
        layout.addRow("Channels:", sources)

        self.rate_box = QtWidgets.QComboBox()
        for depth in range(MAX_DEPTH + 1 if decimation_available() else 1):
            self.rate_box.addItem(format_frequency(1 / (sample_period * (1 << depth))),
                                  1 << depth)
        if decimation_available():
            self.rate_box.setToolTip(
                "Sample rate of the recording: the full rate of the stream, or lower, "
                f"low-pass filtered (flat up to {PASSBAND:g} times the rate) and "
                "decimated by a power of two")
        else:
            self.rate_box.setEnabled(False)
            self.rate_box.setToolTip(
                "Recording at lower sample rates needs the dsp dependency group (see the "
                "README)")
        self.rate_box.currentIndexChanged.connect(self._update)
        layout.addRow("Sample rate:", self.rate_box)

        limit = QtWidgets.QHBoxLayout()
        self.limit_box = QtWidgets.QCheckBox("Stop after")
        self.limit_box.setToolTip(
            "Stop the recording by itself once it holds this much data (rounded to whole "
            "samples of the recording)")
        self.limit_box.toggled.connect(self._update)
        limit.addWidget(self.limit_box)
        self.limit_value_box = QtWidgets.QSpinBox()
        self.limit_value_box.setRange(1, 9999)
        self.limit_value_box.valueChanged.connect(self._update)
        limit.addWidget(self.limit_value_box)
        self.limit_unit_box = QtWidgets.QComboBox()
        for name, seconds in TIME_UNITS:
            self.limit_unit_box.addItem(name, seconds)
        self.limit_unit_box.currentIndexChanged.connect(self._update)
        limit.addWidget(self.limit_unit_box)
        limit.addStretch()
        duration_label = QtWidgets.QLabel("Duration:")
        # Centred on the row rather than at its top, as `QFormLayout` puts labels: on
        # macOS, the row is taller than the others, as the box layout leaves room for the
        # focus ring of the combo box.
        duration_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                                     QtWidgets.QSizePolicy.Policy.Expanding)
        layout.addRow(duration_label, limit)

        self.data_rate_label = QtWidgets.QLabel()
        layout.addRow("Data rate:", self.data_rate_label)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.record_button = buttons.addButton(
            "Record…", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        self.record_button.setToolTip("Choose the file to record to, and start")
        self.record_button.setDefault(True)
        # Otherwise, it would become the default when it has the focus (which it has
        # first on macOS, where check boxes take none by default).
        buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel).setAutoDefault(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.load()

    def load(self):
        """Show the choice remembered from the last recording (of any window)."""
        settings = QtCore.QSettings()
        selected = settings.value("recorder/sources", ",".join(self._names),
                                  str).split(",")
        for name, box in zip(self._names, self.source_boxes):
            box.setChecked(name in selected)
        index = self.rate_box.findData(settings.value("recorder/decimation", 1, int))
        self.rate_box.setCurrentIndex(max(index, 0))
        self.limit_box.setChecked(
            settings.value("recorder/time_limit_enabled", False, bool))
        self.limit_value_box.setValue(settings.value("recorder/time_limit", 1, int))
        index = self.limit_unit_box.findText(
            settings.value("recorder/time_limit_unit", "min", str))
        self.limit_unit_box.setCurrentIndex(max(index, 0))
        self._update()

    def accept(self):
        settings = QtCore.QSettings()
        settings.setValue("recorder/sources",
                          ",".join(self._names[i] for i in self.sources()))
        settings.setValue("recorder/decimation", self.decimation())
        settings.setValue("recorder/time_limit_enabled", self.limit_box.isChecked())
        settings.setValue("recorder/time_limit", self.limit_value_box.value())
        settings.setValue("recorder/time_limit_unit", self.limit_unit_box.currentText())
        super().accept()

    def sources(self) -> list[int]:
        """The indices of the sources selected."""
        return [i for i, box in enumerate(self.source_boxes) if box.isChecked()]

    def decimation(self) -> int:
        """The ratio of the sample rates of the stream and the recording."""
        return self.rate_box.currentData()

    def time_limit(self) -> float | None:
        """The duration after which to stop, in seconds, or `None` for no limit."""
        if not self.limit_box.isChecked():
            return None
        return self.limit_value_box.value() * self.limit_unit_box.currentData()

    def _update(self):
        itemsize = 2 if self.decimation() == 1 else 4  # int16 or float32
        rate = itemsize * len(self.sources()) / (self._sample_period * self.decimation())
        limit = self.time_limit()
        if limit is None:
            total = f"{format_size(3600 * rate)}/h"
        else:
            total = f"{format_size(limit * rate)} in total"
        self.data_rate_label.setText(f"{format_size(rate)}/s ({total})")
        self.limit_value_box.setEnabled(limit is not None)
        self.limit_unit_box.setEnabled(limit is not None)
        self.record_button.setEnabled(bool(self.sources()))


class RecordBar(QtWidgets.QWidget):
    """Records the stream data to an HDF5 file, at the full sample rate or decimated (see
    `StreamRecorder`): *Record…* asks for the sources and the sample rate (`dialog`), and
    then for the file; while recording, the bar shows the progress."""

    def __init__(self, parser: Parser, sample_period: float, parent=None):
        super().__init__(parent)
        self._parser = parser
        self._sample_period = sample_period
        self._stream_thread = None
        self._device = ""
        self._settings: Callable[[], dict] = dict
        self._recorder: StreamRecorder | None = None
        self._stopping = False
        #: What to show about the previous recording, and its path.
        self._summary: tuple[str, str] | None = None

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.record_button = QtWidgets.QPushButton()
        self.record_button.setToolTip(
            "Record channels of the stream to an HDF5 file, at the full sample rate or "
            "lower")
        layout.addWidget(self.record_button)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        # Clip long file names, rather than widening the window.
        self.status_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                        QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self.status_label, 1)

        self.dialog = RecordDialog(parser, sample_period, self)
        self.dialog.accepted.connect(self._start)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(UPDATE_INTERVAL)
        self._timer.timeout.connect(self._update)
        self.record_button.clicked.connect(self._record_clicked)
        self._update()

    def set_stream_thread(self, stream_thread, device: str, settings: Callable[[], dict]):
        """Enable recording from the given `StreamThread`.

        :param settings: Returns a snapshot of the current settings to store.
        """
        self._stream_thread = stream_thread
        self._device = device
        self._settings = settings
        self._update()

    def set_device(self, device: str):
        """Name the files after `device` (the name of the device)."""
        self._device = device

    @property
    def recorder(self) -> StreamRecorder | None:
        """The current recording."""
        return self._recorder

    def _record_clicked(self):
        if self._recorder is None:
            self.dialog.load()
            self.dialog.open()
        elif not self._stopping:
            self._stopping = True
            self._stream_thread.stop_recording()
            self._update()

    def _start(self):
        sources = self.dialog.sources()
        if not sources or self._stream_thread is None:
            return
        settings = QtCore.QSettings()
        directory = settings.value("recorder/directory", os.getcwd(), str)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H_%M_%S")
        name = re.sub(r"[^\w\-.]", "_", f"stream_{self._device}_{timestamp}") + ".h5"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Record stream",
                                                        os.path.join(directory, name),
                                                        "HDF5 files (*.h5)")
        if not path:
            return
        settings.setValue("recorder/directory", os.path.dirname(path))
        self.start_recording(path, sources, self.dialog.decimation(),
                             self.dialog.time_limit())

    def start_recording(self,
                        path: str,
                        sources: list[int],
                        decimation: int = 1,
                        time_limit: float | None = None):
        """Start recording the given sources (indices) to `path`, decimated by
        `decimation`, until stopped or for `time_limit` seconds (see
        `StreamRecorder`)."""
        try:
            recorder = StreamRecorder(path,
                                      self._parser,
                                      sources,
                                      self._sample_period,
                                      self._device,
                                      self._settings(),
                                      decimation=decimation,
                                      time_limit=time_limit)
        except Exception as e:
            logger.exception("Failed to create %s", path)
            QtWidgets.QMessageBox.warning(self, "Recording failed",
                                          f"Failed to create {path}:\n{e}")
            return
        limit = ""
        if recorder.time_limit is not None:
            limit = f" for {format_duration(recorder.time_limit)}"
        logger.info("Recording %s at %s%s to %s", ", ".join(recorder.names),
                    format_frequency(1 / (self._sample_period * decimation)), limit, path)
        self._recorder = recorder
        self._stopping = False
        self._summary = None
        self._stream_thread.start_recording(recorder)
        self._timer.start()
        self._update()

    def _update(self):
        recorder = self._recorder
        if recorder is not None and recorder.finished:
            # Stopped by the user, or by itself.
            self._stream_thread.stop_recording()
            self._timer.stop()
            self._recorder = None
            self._summary = self._describe_finished(recorder)
            logger.info("Recorded %s to %s", format_duration(recorder.duration),
                        recorder.path)
        recording = self._recorder is not None

        if recording:
            self.record_button.setText("Stop")
            self.record_button.setIcon(self.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_MediaStop))
            self.record_button.setEnabled(not self._stopping)
        else:
            self.record_button.setText("Record…")
            self.record_button.setIcon(_record_icon())
            self.record_button.setEnabled(self._stream_thread is not None)

        if recording:
            text = self._describe_recording(self._recorder)
            path = self._recorder.path
        elif self._summary is not None:
            text, path = self._summary
        else:
            text, path = "", None
        self.status_label.setText(text)
        self.status_label.setToolTip(path or "")

    def _describe_recording(self, recorder: StreamRecorder) -> str:
        if self._stopping:
            return "Finishing…"
        if recorder.last_data is None:
            return "Waiting for stream data…"
        text = f"Recording {format_duration(recorder.duration)}"
        if recorder.time_limit is not None:
            text += f" of {format_duration(recorder.time_limit)}"
        text += f", {format_size(recorder.size)}"
        if recorder.lost:
            text += f", {100 * recorder.lost / recorder.samples:.3g} % lost"
        silent = time.monotonic() - recorder.last_data
        if silent > NO_DATA_WARNING:
            text += _warning(f", no stream data for {format_duration(silent)}")
        return f"{text} to {html.escape(os.path.basename(recorder.path))}"

    def _describe_finished(self, recorder: StreamRecorder) -> tuple[str, str]:
        """What to show about a finished recording, and its path."""
        text = (f"Recorded {format_duration(recorder.duration)}, "
                f"{format_size(recorder.size)}")
        if recorder.lost:
            text += f" ({100 * recorder.lost / recorder.samples:.3g} % lost)"
        text += f" to {html.escape(os.path.basename(recorder.path))}"
        if recorder.error is not None:
            text = _warning(f"{text}; stopped: {html.escape(recorder.error)}")
        return text, recorder.path
