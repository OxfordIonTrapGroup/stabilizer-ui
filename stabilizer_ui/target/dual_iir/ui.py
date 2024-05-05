from PyQt5 import QtWidgets
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD
from stabilizer.stream import Parser, AdcDecoder, DacDecoder

from ...widgets import AbstractUiWindow
from ...mqtt import NetworkAddress, UiMqttConfig
from ...iir.channel_settings import ChannelSettings
from ...iir.filters import FILTERS, get_filter
from ...stream.fft_scope import FftScope
from ...utils import kilo, kilo2, link_spinbox_to_is_inf_checkbox
from . import *

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
        self.channels = [ChannelSettings(DEFAULT_DUAL_IIR_SAMPLE_PERIOD) for _ in range(NUM_CHANNELS)]

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
            self.fftScopeWidget.graphics_view.getItem(0, i).setYRange(*DEFAULT_ADC_PLOT_YRANGE)
            self.fftScopeWidget.graphics_view.getItem(1, i).setYRange(*DEFAULT_DAC_PLOT_YRANGE)

        # Disable mouse wheel scrolling on spinboxes to prevent accidental changes
        spinboxes = self.channelTabWidget.findChildren(QtWidgets.QDoubleSpinBox)
        for box in spinboxes:
            box.wheelEvent = lambda *event: None

        self.resize(*DEFAULT_WINDOW_SIZE)

    def update_stream(self, payload):
        self.fftScopeWidget.update(payload)

    async def update_transfer_function(self, setting, all_values):
        if setting.split("/")[0] == "ui":
            channel, iir = setting.split("/")[1:3]
            path_root = f"ui/{channel}/{iir}/"
            filter_type = all_values[path_root + "filter"]
            filter = get_filter(filter_type)

            if filter_type in ["though", "block"]:
                ba = filter.get_coefficients()
            else:
                filter_params = {
                    param: all_values[path_root + f"{filter_type}/{param}"]
                    for param in filter.parameters
                }
                ba = filter.get_coefficients(self.fftScopeWidget.sample_period, **filter_params)

            _iir_widget = self.channels[int(channel)].iir_widgets[int(iir)]
            _iir_widget.update_transfer_function(ba)

    def set_mqtt_configs(self, stream_target: NetworkAddress):
        # `ui/#` are only used by the UI, the others by both UI and stabilizer
        settings_map = {
            "settings/stream_target":
            UiMqttConfig(
                [],
                lambda _: stream_target._asdict(),
                lambda _w, _v: stream_target._asdict(),
            )
        }

        for ch in range(NUM_CHANNELS):
            settings_map[f"settings/afe/{ch}"] = UiMqttConfig(
                [self.channels[ch].afeGainBox])
            for iir in range(NUM_IIR_FILTERS_PER_CHANNEL):
                name_root = f"ui/{ch}/{iir}/"
                iir_ui = self.channels[ch].iir_widgets[iir]
                settings_map[name_root + "filter"] = UiMqttConfig([iir_ui.filterComboBox])
                settings_map[name_root + "x_offset"] = UiMqttConfig([iir_ui.x_offsetBox])
                settings_map[name_root + "y_offset"] = UiMqttConfig([iir_ui.y_offsetBox])
                settings_map[name_root + "y_max"] = UiMqttConfig([iir_ui.y_maxBox])
                settings_map[name_root + "y_min"] = UiMqttConfig([iir_ui.y_minBox])
                for filter in FILTERS:
                    f_str = filter.filter_type
                    for arg in filter.parameters:
                        if arg.split("_")[-1] == "limit":
                            settings_map[name_root + f"{f_str}/{arg}"] = UiMqttConfig(
                                [
                                    getattr(iir_ui.widgets[f_str], f"{arg}Box"),
                                    getattr(iir_ui.widgets[f_str], f"{arg}IsInf"),
                                ],
                                *link_spinbox_to_is_inf_checkbox(),
                            )
                        else:
                            if arg == "f0":
                                settings_map[name_root + f"{f_str}/{arg}"] = UiMqttConfig(
                                    [getattr(iir_ui.widgets[f_str], f"{arg}Box")], *kilo)
                            elif arg == "Ki":
                                settings_map[name_root + f"{f_str}/{arg}"] = UiMqttConfig(
                                    [getattr(iir_ui.widgets[f_str], f"{arg}Box")], *kilo)
                            elif arg == "Kii":
                                settings_map[name_root + f"{f_str}/{arg}"] = UiMqttConfig(
                                    [getattr(iir_ui.widgets[f_str], f"{arg}Box")], *kilo2)
                            else:
                                settings_map[name_root + f"{f_str}/{arg}"] = UiMqttConfig(
                                    [getattr(iir_ui.widgets[f_str], f"{arg}Box")])

        return settings_map
