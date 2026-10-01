from PyQt6 import QtWidgets
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD
from stabilizer.stream_parser import Parser, AdcDecoder

from . import *
from .topics import StabilizerSettings, UiSettings

from ...ui import AbstractUiWindow
from ...mqtt import NetworkAddress, UiMqttConfig
from ...iir.channel_settings import ChannelSettings
from ...stream.fft_scope import FftScope
from ...stream.decoders import DacDecoder
from ...transfer_function.dialog import TransferFunctionWindow


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
        splitter = QtWidgets.QSplitter(self)
        self.setCentralWidget(splitter)

        # Create UI for channel settings.
        self.channels = [
            ChannelSettings(DEFAULT_DUAL_IIR_SAMPLE_PERIOD) for _ in range(NUM_CHANNELS)
        ]

        self.channelTabWidget = QtWidgets.QTabWidget()
        for i, channel in enumerate(self.channels):
            self.channelTabWidget.addTab(channel, f"Channel {i}")
        splitter.addWidget(self.channelTabWidget)

        # Create UI for FFT scope.
        streamParser = Parser([AdcDecoder(), DacDecoder()])
        self.fftScopeWidget = FftScope(streamParser, DEFAULT_DUAL_IIR_SAMPLE_PERIOD)
        splitter.addWidget(self.fftScopeWidget)

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

        self._settings_map = {}
        self._sweep_runner = None
        self._transfer_function_window = None
        tools_menu = self.menuBar().addMenu("&Tools")
        self.transferFunctionAction = tools_menu.addAction("&Transfer function…")
        self.transferFunctionAction.setShortcut("Ctrl+T")
        self.transferFunctionAction.setEnabled(False)
        self.transferFunctionAction.triggered.connect(self.show_transfer_function)

    def set_sweep_runner(self, runner):
        """Enable transfer function measurements using the given `SweepRunner`."""
        self._sweep_runner = runner
        self.transferFunctionAction.setEnabled(True)

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
        settings = {
            key: cfg.read_handler(cfg.widgets)
            for key, cfg in self._settings_map.items()
        }
        gains = self.afe_gains()
        settings["afe_gains"] = {str(ch): gain for ch, gain in enumerate(gains)}
        settings["biquads"] = {
            str(ch): channel.iir_widgets[0].coefficients
            for ch, channel in enumerate(self.channels)
        }
        return settings

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

        self._settings_map = settings_map
        return settings_map
