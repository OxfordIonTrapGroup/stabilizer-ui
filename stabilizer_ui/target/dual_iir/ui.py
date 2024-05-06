from PyQt5 import QtWidgets
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD
from stabilizer.stream import Parser, AdcDecoder, DacDecoder

from . import *
from .topics import StabilizerSettings, UiSettings

from ...ui import AbstractUiWindow
from ...mqtt import NetworkAddress, UiMqttConfig
from ...iir.channel_settings import ChannelSettings
from ...iir.filters import FILTERS, get_filter
from ...stream.fft_scope import FftScope
from ...utils import kilo, kilo2, link_spinbox_to_is_inf_checkbox

#
# Parameters for the FNC ui.
#

DEFAULT_WINDOW_SIZE = (1200, 600)
DEFAULT_DAC_PLOT_YRANGE = (-1, 1)
DEFAULT_ADC_PLOT_YRANGE = (-1, 1)

#: Interval between scope plot updates, in seconds.
#: PyQt's drawing speed limits value.
SCOPE_UPDATE_PERIOD = 0.05  # 20 fps


class UiWindow(AbstractUiWindow):

    def __init__(self, title: str = "Dual IIR"):
        super().__init__()
        self.setWindowTitle(title)

        # Set main window layout
        self.setCentralWidget(QtWidgets.QWidget(self))
        centralLayout = QtWidgets.QHBoxLayout(self.centralWidget())

        # Create UI for channel settings.
        self.channels = [
            ChannelSettings(DEFAULT_DUAL_IIR_SAMPLE_PERIOD) for _ in range(NUM_CHANNELS)
        ]

        self.channelTabWidget = QtWidgets.QTabWidget()
        for i, channel in enumerate(self.channels):
            self.channelTabWidget.addTab(channel, f"Channel {i}")
        centralLayout.addWidget(self.channelTabWidget)

        # Create UI for FFT scope.
        streamParser = Parser([AdcDecoder(), DacDecoder()])
        self.fftScopeWidget = FftScope(streamParser, DEFAULT_DUAL_IIR_SAMPLE_PERIOD)
        centralLayout.addWidget(self.fftScopeWidget)

        # # Give any excess space to the FFT scope
        # centralLayout.setStretchFactor(self.fftScopeWidget, 1)

        for i in range(NUM_CHANNELS):
            self.fftScopeWidget.graphics_view.getItem(
                0, i).setYRange(*DEFAULT_ADC_PLOT_YRANGE)
            self.fftScopeWidget.graphics_view.getItem(
                1, i).setYRange(*DEFAULT_DAC_PLOT_YRANGE)

        # Disable mouse wheel scrolling on spinboxes to prevent accidental changes
        spinboxes = self.channelTabWidget.findChildren(QtWidgets.QDoubleSpinBox)
        for box in spinboxes:
            box.wheelEvent = lambda *event: None

        self.resize(*DEFAULT_WINDOW_SIZE)

    def update_stream(self, payload):
        self.fftScopeWidget.update(payload)

    def set_mqtt_configs(self, stream_target: NetworkAddress):
        """ Link the UI widgets to the MQTT topic tree"""

        # `ui/#` are only used by the UI, the others by both UI and stabilizer
        settings_map = {
            StabilizerSettings.stream_target.path():
            UiMqttConfig(
                [],
                lambda _: stream_target._asdict(),
                lambda _w, _v: stream_target._asdict(),
            )
        }

        for ch in range(NUM_CHANNELS):
            settings_map[StabilizerSettings.afes[ch].path()] = UiMqttConfig(
                [self.channels[ch].afeGainBox])

            # IIR settings
            for iir in range(NUM_IIR_FILTERS_PER_CHANNEL):
                iirWidget = self.channels[ch].iir_widgets[iir]
                iir_topic = UiSettings.iirs[ch][iir]

                iirWidget.set_mqtt_configs(settings_map, iir_topic)

        return settings_map
