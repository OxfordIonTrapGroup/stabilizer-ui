import math
import os
from functools import cached_property

from PyQt6 import QtCore, QtWidgets, uic
from stabilizer.stream_parser import Parser
import numpy as np
import numpy.fft
from .thread import CallbackPayload

from . import DEFAULT_SCOPE_DURATION, MAX_SCOPE_DURATION, SCOPE_TIME_SCALE

#: Interval between scope plot updates, in seconds.
#: PyQt's drawing speed limits value.
DEFAULT_SCOPE_UPDATE_PERIOD = 0.05  # 20 fps

#: Resolution of the spectrum shown, in points per decade. The bins within each interval
#: are shown as their minimum and maximum, as drawing all of them would be slow for long
#: traces.
SPECTRUM_POINTS_PER_DECADE = 2000

GRAPHICSLAYOUT_BORDER_WIDTH = 0.2
LEGEND_OFFSET = (-10, 10)


class ScopeConfig:
    """What the scope shows: the length of the traces, and whether as time series or
    amplitude spectral density.

    The stream thread uses the configuration of the scope at the time to compute the plot
    data (`precondition()`), which the scope only shows if it is still current.
    """

    def __init__(self, length: int, sample_period: float, fft: bool):
        #: Number of samples of each trace.
        self.length = length
        self.sample_period = sample_period
        self.fft = fft

    @property
    def duration(self) -> float:
        return self.length * self.sample_period

    @cached_property
    def sample_times(self) -> np.ndarray:
        return np.linspace(-self.duration, 0, self.length) / SCOPE_TIME_SCALE

    @cached_property
    def _window(self) -> tuple[np.ndarray, float]:
        """The window, and the factor to scale the spectrum to an amplitude spectral
        density (one-sided)."""
        window = np.hamming(self.length).astype(np.float32)
        return window, math.sqrt(2 * self.sample_period / np.sum(window.astype(float)**2))

    @cached_property
    def frequencies(self) -> np.ndarray:
        return np.fft.rfftfreq(self.length, self.sample_period)

    @cached_property
    def _envelope_starts(self) -> np.ndarray:
        """Index of the first bin of each interval of the spectrum (without DC) shown as
        one point."""
        group = np.floor(np.log10(self.frequencies[1:]) * SPECTRUM_POINTS_PER_DECADE)
        return np.flatnonzero(np.diff(group, prepend=-np.inf)) + 1

    def frequency_range(self) -> tuple[float, float]:
        """The range of bins (other than DC), as decimal logarithm of the frequency."""
        return (math.log10(self.frequencies[1]), math.log10(self.frequencies[-1]))

    def precondition(self, data: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
        """Transform data (source, sample) into what `FftScope.update()` shows."""
        if not self.fft:
            return [(self.sample_times, trace) for trace in data]
        window, scale = self._window
        starts = self._envelope_starts
        x = np.repeat(self.frequencies[starts], 2)
        result = []
        for trace in data:
            spectrum = np.abs(np.fft.rfft(trace * window)) * scale
            y = np.empty(2 * len(starts), spectrum.dtype)
            y[0::2] = np.minimum.reduceat(spectrum, starts)
            y[1::2] = np.maximum.reduceat(spectrum, starts)
            result.append((x, y))
        return result


class DurationBox(QtWidgets.QDoubleSpinBox):
    """Spin box which steps through 1, 2 and 5 times powers of ten, like the time base of
    an oscilloscope."""

    MANTISSAS = (1, 2, 5)

    def stepBy(self, steps):
        value = self.value()
        exponent = math.floor(math.log10(value))
        index = max(i for i, m in enumerate(self.MANTISSAS)
                    if m * 10**exponent <= value * (1 + 1e-9))
        k = 3 * exponent + index
        on_grid = math.isclose(value, self.MANTISSAS[index] * 10**exponent)
        # From between two values, the first step down goes to the lower one.
        k += steps if steps > 0 or on_grid else steps + 1
        self.setValue(self.MANTISSAS[k % 3] * 10.0**(k // 3))


class FftScope(QtWidgets.QWidget):
    DEFAULT_Y_RANGE = (-11, 11)
    DEFAULT_FFT_Y_RANGE = (-7, -1)

    def __init__(self,
                 parser: Parser,
                 sample_period: float,
                 update_period=DEFAULT_SCOPE_UPDATE_PERIOD):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "scope.ui")
        uic.loadUi(ui_path, self)

        self.stream_parser = parser
        self.sample_period = sample_period
        self.update_period = update_period
        self._stream_active = True

        self.scope_plot_items = [
            self.graphics_view.addPlot(row=i, col=j) for i in range(2) for j in range(2)
        ]
        # Maximise space utilisation.
        self.graphics_view.ci.layout.setContentsMargins(0, 0, 0, 0)
        self.graphics_view.ci.layout.setSpacing(0)
        self.graphics_view.centralWidget.setBorder(width=GRAPHICSLAYOUT_BORDER_WIDTH)

        # Use legend instead of title to save space.
        legends = [plt.addLegend(offset=LEGEND_OFFSET) for plt in self.scope_plot_items]
        # Create the objects holding the data to plot. When downsampling, show the minimum
        # and maximum for each pixel (instead of five), as Qt drops lines between points
        # closer than a pixel (e.g. for noise of a few LSB).
        self._scope_plot_data_items = [
            plt.plot(autoDownsampleFactor=1.0) for plt in self.scope_plot_items
        ]
        for legend, item, title in zip(legends, self._scope_plot_data_items,
                                       parser.StreamData._fields):
            legend.addItem(item, title)

        # Maps `self.en_fft_box.isChecked()` to a dictionary of axis settings.
        self.scope_config = [{
            True: {
                "ylabel": f"ASD / ({unit}/√Hz)",
                "xlabel": "Frequency / Hz",
                "log": [True, True],
                "yrange": self.DEFAULT_FFT_Y_RANGE,
            },
            False: {
                "ylabel": f"Amplitude / {unit}",
                "xlabel": "Time / ms",
                "log": [False, False],
                "yrange": self.DEFAULT_Y_RANGE,
            },
        } for unit in parser.units()]

        self.duration_box.setMaximum(MAX_SCOPE_DURATION / SCOPE_TIME_SCALE)
        settings = QtCore.QSettings()
        try:
            duration = settings.value("scope/duration", DEFAULT_SCOPE_DURATION, float)
            fft = settings.value("scope/fft", False, bool)
        except (TypeError, ValueError):
            duration, fft = DEFAULT_SCOPE_DURATION, False
        self.duration_box.setValue(duration / SCOPE_TIME_SCALE)
        self.en_fft_box.setChecked(fft)
        self._config = self._make_config()
        self._update_axes(True)

        self.en_fft_box.stateChanged.connect(lambda _: self._config_changed(True))
        self.duration_box.valueChanged.connect(lambda _: self._config_changed(False))

    @property
    def config(self) -> ScopeConfig:
        """The current configuration (read by the stream thread)."""
        return self._config

    def _make_config(self) -> ScopeConfig:
        length = max(
            2, round(self.duration_box.value() * SCOPE_TIME_SCALE / self.sample_period))
        return ScopeConfig(length, self.sample_period, self.en_fft_box.isChecked())

    def _config_changed(self, mode_changed: bool):
        self._config = self._make_config()
        self._update_axes(mode_changed)
        settings = QtCore.QSettings()
        settings.setValue("scope/duration", self._config.duration)
        settings.setValue("scope/fft", self._config.fft)

    def _update_axes(self, mode_changed: bool):
        """Show the time or frequency range of the traces, and if the mode has changed,
        set its axes."""
        config = self._config
        if config.fft:
            xrange = config.frequency_range()
        else:
            xrange = (-config.duration / SCOPE_TIME_SCALE, 0)
        for i, plt in enumerate(self.scope_plot_items):
            if mode_changed:
                cfg = self.scope_config[i][config.fft]
                plt.setLogMode(*cfg["log"])
                plt.setLabels(left=cfg["ylabel"], bottom=cfg["xlabel"])
                plt.setYRange(*cfg["yrange"])
            plt.setXRange(*xrange, padding=0)
        for item in self._scope_plot_data_items:
            # The spectrum is reduced already (`ScopeConfig.precondition()`), and the
            # automatic downsampling assumes evenly spaced points.
            item.setDownsampling(auto=not config.fft, method="peak")
            item.setClipToView(not config.fft)

    def set_stream_active(self, active: bool):
        """Stop showing data while the stream is not directed here, as the stream thread
        keeps reporting the last data it has received."""
        self._stream_active = active
        if not active:
            self.status_line.setText("No stream")
            for plot in self._scope_plot_data_items:
                plot.setData([], [])

    def update(self, payload: CallbackPayload):
        """Callback for the stream thread"""
        if not self._stream_active:
            return
        message = "Speed: {:.2f} MB/s ({:.3f} % batches lost)".format(
            payload.download / 1e6, 100 * payload.loss)
        self.status_line.setText(message)

        if payload.values is None or payload.config is not self._config:
            return
        for plot, data in zip(self._scope_plot_data_items, payload.values):
            plot.setData(*data)
