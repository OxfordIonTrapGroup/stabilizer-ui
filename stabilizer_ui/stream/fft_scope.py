import os
from PyQt5 import QtWidgets, uic
from stabilizer import SAMPLE_PERIOD
from collections import namedtuple
from stabilizer.stream import Parser
import numpy as np
import numpy.fft
from math import floor
from typing import Iterable
from .thread import CallbackPayload

from . import MAX_BUFFER_PERIOD, SCOPE_TIME_SCALE


class FftScope(QtWidgets.QWidget):
    def __init__(self, StreamData: namedtuple):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "scope.ui")
        uic.loadUi(ui_path, self)

        scope_plot_items = [
            self.graphics_view.addPlot(row=i, col=j) for i in range(2) for j in range(2)
        ]
        # Maximise space utilisation.
        self.graphics_view.ci.layout.setContentsMargins(0, 0, 0, 0)
        self.graphics_view.ci.layout.setSpacing(0)
        # Use legend instead of title to save space.
        legends = [plt.addLegend(offset=(-10, 10)) for plt in scope_plot_items]
        # Create the objects holding the data to plot.
        self._scope_plot_data_items = [plt.plot() for plt in scope_plot_items]
        for legend, item, title in zip(legends, self._scope_plot_data_items,
                                       StreamData._fields):
            legend.addItem(item, title)

        # Maps `self.en_fft_box.isChecked()` to a dictionary of axis settings.
        self.scope_config = {
            True: {
                "ylabel": "ASD / (V/sqrt(Hz))",
                "xlabel": "Frequency / kHz",
                "log": [True, True],
                "xrange": [0.5, np.log10(0.5 * SCOPE_TIME_SCALE / SAMPLE_PERIOD)],
                "yrange": [-7, -1],
            },
            False: {
                "ylabel": "Amplitude / V",
                "xlabel": "Time / ms",
                "log": [False, False],
                "xrange": [-MAX_BUFFER_PERIOD / SCOPE_TIME_SCALE, 0],
                "yrange": [-11, 11],
            },
        }

        def update_axes(button_checked):
            cfg = self.scope_config[bool(button_checked)]
            for plt in scope_plot_items:
                plt.setLogMode(*cfg["log"])
                plt.setRange(xRange=cfg["xrange"], yRange=cfg["yrange"], update=False)
                plt.setLabels(left=cfg["ylabel"], bottom=cfg["xlabel"])

        self.buf_len = int(MAX_BUFFER_PERIOD / SAMPLE_PERIOD)
        self.sample_times = np.linspace(-self.buf_len * SAMPLE_PERIOD, 0, self.buf_len) / SCOPE_TIME_SCALE
        self.hamming = np.hamming(self.buf_len)
        self.spectrum_frequencies = np.linspace(0, 0.5 / SAMPLE_PERIOD, floor((self.buf_len+1)/2)) * SCOPE_TIME_SCALE

        self.en_fft_box.stateChanged.connect(update_axes)
        update_axes(self.en_fft_box.isChecked())

    def update(self, payload: CallbackPayload):
        """Callback for the stream thread"""
        message = "Speed: {:.2f} MB/s ({:.3f} % batches lost)".format(
            payload.download / 1e6, 100 * payload.loss)
        self.status_line.setText(message)

        data_to_show = payload.values
        for plot, data in zip(self._scope_plot_data_items, data_to_show):
            plot.setData(*data)

    def precondition_data(self):
        """Transforms data into payload values recognised by `update()`"""
        def _preconditioner(data: Iterable):
            if self.en_fft_box.isChecked():
                return [(self.spectrum_frequencies, np.abs(np.fft.rfft(buf * self.hamming)) * np.sqrt(2 * SAMPLE_PERIOD / self.buf_len)) for buf in data]
            else:
                return [(self.sample_times, buf) for buf in data]

        return _preconditioner
