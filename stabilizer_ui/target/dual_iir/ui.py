from PyQt5 import QtWidgets
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD
from stabilizer.stream import Parser, AdcDecoder, DacDecoder

from ...widgets import AbstractUiWindow
from ...mqtt import NetworkAddress, UiMqttConfig
from ...iir.channel_settings import ChannelSettings
from ...iir.filters import FILTERS
from ...stream.fft_scope import FftScope
from ...utils import kilo, kilo2, link_spinbox_to_is_inf_checkbox


class UiWindow(AbstractUiWindow):

    def __init__(self, title: str = "Dual IIR"):
        super().__init__()

        self.setWindowTitle(title)

        wid = QtWidgets.QWidget(self)
        self.setCentralWidget(wid)
        layout = QtWidgets.QHBoxLayout()

        # Create UI for channel settings.
        self.channel_settings = [ChannelSettings(DEFAULT_DUAL_IIR_SAMPLE_PERIOD) for i in range(2)]

        self.tab_channel_settings = QtWidgets.QTabWidget()
        for i, channel in enumerate(self.channel_settings):
            self.tab_channel_settings.addTab(channel, f"Channel {i}")
        layout.addWidget(self.tab_channel_settings)

        # Create UI for FFT scope.
        streamParser = Parser([AdcDecoder(), DacDecoder()])
        self.fft_scope = FftScope(streamParser, DEFAULT_DUAL_IIR_SAMPLE_PERIOD)
        layout.addWidget(self.fft_scope)

        # Set main window layout
        wid.setLayout(layout)

    def update_stream(self, payload):
        # if self.tab_channel_settings.currentIndex() != 1:
        #    return
        self.fft_scope.update(payload)

    async def update_transfer_function(self, setting, all_values):
        if setting.split("/")[0] == "ui":
            channel, iir = setting.split("/")[1:3]
            path_root = f"ui/{channel}/{iir}/"
            filter_type = all_values[path_root + "filter"]
            filter_idx = [f.filter_type for f in FILTERS].index(filter_type)
            kwargs = {
                param: all_values[path_root + f"{filter_type}/{param}"]
                for param in FILTERS[filter_idx].parameters
            }
            ba = FILTERS[filter_idx].get_coefficients(self.fft_scope.sample_period, **kwargs)
            _iir_widget = self.channel_settings[int(channel)].iir_widgets[int(iir)]
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

        for c in range(2):
            settings_map[f"settings/afe/{c}"] = UiMqttConfig(
                [self.channel_settings[c].afeGainBox])
            for iir in range(2):
                name_root = f"ui/{c}/{iir}/"
                iir_ui = self.channel_settings[c].iir_widgets[iir]
                settings_map[name_root + "filter"] = UiMqttConfig([iir_ui.filterComboBox])
                settings_map[name_root + "x_offset"] = UiMqttConfig([iir_ui.x_offsetBox])
                settings_map[name_root + "y_offset"] = UiMqttConfig([iir_ui.y_offsetBox])
                settings_map[name_root + "y_max"] = UiMqttConfig([iir_ui.y_maxBox])
                settings_map[name_root + "y_min"] = UiMqttConfig([iir_ui.y_minBox])
                for f in FILTERS:
                    f_str = f.filter_type
                    for arg in f.parameters:
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
