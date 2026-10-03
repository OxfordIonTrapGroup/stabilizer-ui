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

from .recorder import StreamRecorder
from ..utils import format_duration, format_size

logger = logging.getLogger(__name__)

#: Interval between updates of the status while recording, in milliseconds.
UPDATE_INTERVAL = 250
#: Time without stream data after which the status mentions it, in seconds.
NO_DATA_WARNING = 1.0


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


class RecordBar(QtWidgets.QWidget):
    """Records the stream data of the checked sources at the full sample rate to an HDF5
    file (see `StreamRecorder`)."""

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
            "Record the checked channels of the stream at the full sample rate to an "
            "HDF5 file")
        layout.addWidget(self.record_button)
        self.source_boxes = []
        selected = self._load_selection()
        for name in parser.StreamData._fields:
            box = QtWidgets.QCheckBox(name)
            box.setToolTip(f"Record {name}")
            box.setChecked(name in selected)
            box.toggled.connect(self._selection_changed)
            layout.addWidget(box)
            self.source_boxes.append(box)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        # Clip long file names, rather than widening the window.
        self.status_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                        QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self.status_label, 1)

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

    def _selected(self) -> list[int]:
        return [i for i, box in enumerate(self.source_boxes) if box.isChecked()]

    def _load_selection(self) -> list[str]:
        names = self._parser.StreamData._fields
        value = QtCore.QSettings().value("recorder/sources", ",".join(names), str)
        return value.split(",")

    def _selection_changed(self):
        names = self._parser.StreamData._fields
        QtCore.QSettings().setValue("recorder/sources",
                                    ",".join(names[i] for i in self._selected()))
        self._summary = None
        self._update()

    def _record_clicked(self):
        if self._recorder is None:
            self._start()
        elif not self._stopping:
            self._stopping = True
            self._stream_thread.stop_recording()
            self._update()

    def _start(self):
        sources = self._selected()
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
        self.start_recording(path, sources)

    def start_recording(self, path: str, sources: list[int]):
        """Start recording the given sources (indices) to `path`."""
        try:
            recorder = StreamRecorder(path, self._parser, sources, self._sample_period,
                                      self._device, self._settings())
        except Exception as e:
            logger.exception("Failed to create %s", path)
            QtWidgets.QMessageBox.warning(self, "Recording failed",
                                          f"Failed to create {path}:\n{e}")
            return
        logger.info("Recording %s to %s", ", ".join(recorder.names), path)
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
            self.record_button.setEnabled(self._stream_thread is not None
                                          and bool(self._selected()))
        for box in self.source_boxes:
            box.setEnabled(not recording)

        if recording:
            text = self._describe_recording(self._recorder)
            path = self._recorder.path
        elif self._summary is not None:
            text, path = self._summary
        else:
            rate = 2 * len(self._selected()) / self._sample_period
            text = f"{format_size(rate)}/s ({format_size(3600 * rate)}/h)"
            path = None
        self.status_label.setText(text)
        self.status_label.setToolTip(path or "")

    def _describe_recording(self, recorder: StreamRecorder) -> str:
        if self._stopping:
            return "Finishing…"
        if recorder.last_data is None:
            return "Waiting for stream data…"
        text = (f"Recording {format_duration(recorder.duration)}, "
                f"{format_size(recorder.size)}")
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
