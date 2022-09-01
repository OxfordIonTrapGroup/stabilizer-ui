from PyQt5 import QtWidgets
from pyqtgraph import GraphicsLayoutWidget
from stabilizer import SAMPLE_PERIOD
from typing import List
import numpy as np
import numpy.fft
from .stream import CallbackPayload, StreamData


class FftScope:
    def __init__(self, graphics_view: GraphicsLayoutWidget,
                 en_fft_box: QtWidgets.QCheckBox, status_line: QtWidgets.QLineEdit):
        self.en_fft_box = en_fft_box
        self.status_line = status_line

        scope_plot_items = [
            graphics_view.addPlot(row=i, col=j) for i in range(2) for j in range(2)
        ]
        # Maximise space utilisation.
        graphics_view.ci.layout.setContentsMargins(0, 0, 0, 0)
        graphics_view.ci.layout.setSpacing(0)
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
                "xrange": [0.5, np.log10(0.5 * MAX_BUFFER_PERIOD / SAMPLE_PERIOD)],
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
                plt.setLogMode(*cfg['log'])
                plt.setRange(xRange=cfg['xrange'], yRange=cfg['yrange'], update=False)
                plt.setLabels(left=cfg['ylabel'], bottom=cfg['xlabel'])

        self.en_fft_box.stateChanged.connect(update_axes)
        update_axes(self.en_fft_box.isChecked())

    def update(self, payload: CallbackPayload):
        """ Callback for the stream thread
        """
        message = "Speed: {:.2f} MB/s ({:.3f} % batches lost)".format(
            payload.download / 1e6, 100 * payload.loss)
        self.status_line.setText(message)

        traces, spectra = payload.values
        data_to_show = spectra if self.en_fft_box.isChecked() else traces
        for plot, data in zip(self._scope_plot_data_items, data_to_show):
            plot.setData(*data)

    @staticmethod
    def precondition_data(data: StreamData):
        """ Transforms data into payload values recognised by `update()`
        """
        traces = [
            (np.linspace(-len(buf) * SAMPLE_PERIOD, 0, len(buf)) / SCOPE_TIME_SCALE,
             buf) for buf in data
        ]
        transforms = [
            np.abs(numpy.fft.rfft(buf)) * np.sqrt(2 * SAMPLE_PERIOD / len(buf))
            for buf in data
        ]
        spectra = [(np.linspace(0, 0.5 / SAMPLE_PERIOD, len(fbuf)) * SCOPE_TIME_SCALE,
                    fbuf) for fbuf in transforms]
        return traces, spectra