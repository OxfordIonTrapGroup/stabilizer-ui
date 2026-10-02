import os
import numpy as np
import pyqtgraph as pg
from PyQt6 import QtWidgets, uic
from stabilizer.stream_parser import Parser, AdcDecoder

from . import *
from .topics import StabilizerSettings, UiSettings

from ...ui import AbstractUiWindow
from ...mqtt import NetworkAddress, UiMqttConfig
from ...iir.channel_settings import AbstractChannelSettings, ChannelSettings
from ...stream.fft_scope import FftScope
from ...spectral_density import SpectralDensityMixin
from ...stream.decoders import DacDecoder
from ...transfer_function.dialog import TransferFunctionMixin
from ...utils import milli

#
# Parameters for the Current sense ui.
#

DEFAULT_WINDOW_SIZE = (1200, 600)
DEFAULT_DAC_PLOT_YRANGE = (-1, 1)
DEFAULT_ADC_PLOT_YRANGE = (-1, 1)

#: Period of the timer ticks the PLL settling times are given in, in seconds.
TIMER_PERIOD = 10e-9


def _read_harmonic(widgets):
    """Expects widgets in the form [amplitude (mV), phase (degrees)]."""
    return {"amp": widgets[0].value() * 1e-3, "phase_turns": widgets[1].value() / 360}


def _write_harmonic(widgets, value):
    """Expects widgets in the form [amplitude (mV), phase (degrees)]."""
    widgets[0].setValue(value["amp"] * 1e3)
    phase = value["phase_turns"] * 360
    if not -180 <= phase <= 180:
        # The device accepts any phase; wrap it instead of clamping to the range.
        phase = (phase + 180) % 360 - 180
    widgets[1].setValue(phase)


harmonic_readwrite = (_read_harmonic, _write_harmonic)


class FeedforwardSettings(QtWidgets.QWidget):
    """ Settings for the feedforward waveform and the mains PLL it is synchronised by"""

    def __init__(self):
        super().__init__()

        uic.loadUi(
            os.path.join(os.path.dirname(os.path.realpath(__file__)),
                         "widgets/feedforward.ui"), self)

        #: The [amplitude, phase] boxes of each harmonic, starting with the fundamental.
        self.harmonic_boxes = []
        for harmonic in range(NUM_HARMONICS):
            amplitudeBox = QtWidgets.QDoubleSpinBox()
            amplitudeBox.setSuffix(" mV")
            amplitudeBox.setDecimals(3)
            amplitudeBox.setRange(-10e3, 10e3)
            amplitudeBox.setSingleStep(0.1)

            phaseBox = QtWidgets.QDoubleSpinBox()
            phaseBox.setSuffix(" °")
            phaseBox.setRange(-180, 180)
            phaseBox.setWrapping(True)

            row = harmonic + 1
            self.harmonicsLayout.addWidget(QtWidgets.QLabel(str(harmonic + 1)), row, 0)
            for column, box in enumerate([amplitudeBox, phaseBox], start=1):
                box.setKeyboardTracking(False)
                box.valueChanged.connect(self._update_waveform)
                self.harmonicsLayout.addWidget(box, row, column)
            self.harmonic_boxes.append([amplitudeBox, phaseBox])

        for box, label in [(self.pllFrequencyTcBox, self.pllFrequencyTcLabel),
                           (self.pllPhaseTcBox, self.pllPhaseTcLabel)]:
            # Capture loop variable.
            def update_settling_time(tc, label=label):
                label.setText(f"≈ {pg.siFormat((1 << tc) * TIMER_PERIOD, suffix='s')}")

            box.valueChanged.connect(update_settling_time)
            update_settling_time(box.value())

        # Preview of one mains period of the waveform
        plot = self.waveformView.addPlot(row=0, col=0)
        plot.setLabel("left", "Feedforward", units="V")
        plot.setLabel("bottom", "Mains phase", units="turns")
        plot.getAxis("bottom").enableAutoSIPrefix(False)
        self._mains_phase = np.linspace(0, 1, 501)
        self._waveform = plot.plot()
        self._update_waveform()

    def _update_waveform(self):
        waveform = sum(
            cfg["amp"] * np.sin(2 * np.pi *
                                (order * self._mains_phase + cfg["phase_turns"]))
            for order, cfg in enumerate(map(_read_harmonic, self.harmonic_boxes),
                                        start=1))
        self._waveform.setData(self._mains_phase, waveform)


class FeedforwardChannelSettings(AbstractChannelSettings):
    """ Channel settings for channel 0: those of a `dual-iir` channel, plus the offsets
    of the current sense board and the mains feedforward
    """
    # DI0 is the mains reference input, so cannot control the hold.
    run_options = ["Run", "Hold"]

    def __init__(self, sample_period):
        super().__init__()

        uic.loadUi(
            os.path.join(os.path.dirname(os.path.realpath(__file__)),
                         "widgets/channel.ui"), self)

        self._add_afe_options()
        self._add_iir_tabWidget(sample_period)

        self.feedforward = FeedforwardSettings()
        self.IIRTabs.addTab(self.feedforward, "Feedforward")


class UiWindow(TransferFunctionMixin, SpectralDensityMixin, AbstractUiWindow):

    def __init__(self, title: str = "Current sense"):
        super().__init__()
        self.setWindowTitle(title)

        # Set main window layout
        splitter = QtWidgets.QSplitter(self)
        self.setCentralWidget(splitter)

        # Create UI for channel settings. The current sense board (and thus the
        # feedforward) is on channel 0; channel 1 is a regular `dual-iir` channel.
        self.channels = [
            FeedforwardChannelSettings(SAMPLE_PERIOD),
            ChannelSettings(SAMPLE_PERIOD)
        ]

        self.channelTabWidget = QtWidgets.QTabWidget()
        for i, channel in enumerate(self.channels):
            self.channelTabWidget.addTab(channel, f"Channel {i}")
        splitter.addWidget(self.channelTabWidget)

        # Create UI for FFT scope.
        streamParser = Parser([AdcDecoder(), DacDecoder()])
        self.fftScopeWidget = FftScope(streamParser, SAMPLE_PERIOD)
        splitter.addWidget(self.fftScopeWidget)

        for i in range(NUM_CHANNELS):
            self.fftScopeWidget.graphics_view.getItem(
                0, i).setYRange(*DEFAULT_ADC_PLOT_YRANGE)
            self.fftScopeWidget.graphics_view.getItem(
                1, i).setYRange(*DEFAULT_DAC_PLOT_YRANGE)

        # Disable mouse wheel scrolling on spinboxes to prevent accidental changes
        spinboxes = self.channelTabWidget.findChildren(QtWidgets.QAbstractSpinBox)
        for box in spinboxes:
            box.wheelEvent = lambda *event: None

        self.resize(*DEFAULT_WINDOW_SIZE)

        self._add_transfer_function_action()
        self._add_spectral_density_action()

    def update_stream(self, payload):
        self.fftScopeWidget.update(payload)

    def set_mqtt_configs(self, stream_target: NetworkAddress):
        """ Link the UI widgets to the MQTT topic tree"""

        # `ui/#` are only used by the UI, the others by both UI and stabilizer
        settings_map = {
            StabilizerSettings.stream.path():
            UiMqttConfig(
                [],
                lambda _: str(stream_target),
                lambda _w, _v: str(stream_target),
            )
        }

        for ch in range(NUM_CHANNELS):
            settings_map[StabilizerSettings.afes[ch].path()] = UiMqttConfig(
                [self.channels[ch].afeGainBox])
            settings_map[StabilizerSettings.runs[ch].path()] = UiMqttConfig(
                [self.channels[ch].runModeBox])

            # IIR settings
            for iir in range(NUM_IIR_FILTERS_PER_CHANNEL):
                iirWidget = self.channels[ch].iir_widgets[iir]
                iir_topic = UiSettings.iirs[ch][iir]

                iirWidget.set_mqtt_configs(settings_map, iir_topic)

        # Current sense board and feedforward settings
        channel = self.channels[0]
        settings_map[StabilizerSettings.frontend_offset.path()] = UiMqttConfig(
            [channel.frontendOffsetBox])
        settings_map[StabilizerSettings.feedback_offset.path()] = UiMqttConfig(
            [channel.feedbackOffsetBox], *milli)

        feedforward = channel.feedforward
        for topic, box in zip(StabilizerSettings.pll_tcs,
                              [feedforward.pllFrequencyTcBox, feedforward.pllPhaseTcBox]):
            settings_map[topic.path()] = UiMqttConfig([box])
        for topic, boxes in zip(StabilizerSettings.feedforward_harmonics,
                                feedforward.harmonic_boxes):
            settings_map[topic.path()] = UiMqttConfig(boxes, *harmonic_readwrite)

        self._settings_map = settings_map
        return settings_map
