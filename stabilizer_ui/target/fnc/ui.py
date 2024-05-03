import logging
from PyQt5 import QtWidgets
from math import inf
from stabilizer import DEFAULT_FNC_SAMPLE_PERIOD
from stabilizer.stream import Parser, AdcDecoder, PhaseOffsetDecoder

from .topics import stabilizer, ui
from .widgets.clock import ClockWidget
from .widgets.channels import ChannelTabWidget
from .parameters import *

from ...stream.fft_scope import FftScope
from ...ui_mqtt_bridge import UiMqttConfig, NetworkAddress
from ... import ui_mqtt_bridge
from ...iir.filters import FILTERS, get_filter
from ...widgets.ui import AbstractUiWindow

logger = logging.getLogger(__name__)


class UiWindow(AbstractUiWindow):
    """ Main UI window for FNC"""

    def __init__(self):
        super().__init__()

        # Set up main window with Horizontal layout
        self.setCentralWidget(QtWidgets.QWidget(self))
        self.centralWidget = self.centralWidget()
        self.centralWidgetLayout = QtWidgets.QHBoxLayout(self.centralWidget)

        # Add FFT scope next to a vertical layout for settings
        self.settingsLayout = QtWidgets.QVBoxLayout()

        fftParser = Parser([AdcDecoder(), PhaseOffsetDecoder()])
        self.fftScopeWidget = FftScope(fftParser, DEFAULT_FNC_SAMPLE_PERIOD)
        self.centralWidgetLayout.addLayout(self.settingsLayout)
        self.centralWidgetLayout.addWidget(self.fftScopeWidget)

        self.channelTabWidget = ChannelTabWidget()
        self.channels = self.channelTabWidget.channels
        self.clockWidget = ClockWidget()

        # As of 23/04/2024, updating the clock at runtime causes timing issues.
        # Disable the clock widget until this is resolved.
        self.clockWidget.setEnabled(False)

        self.settingsLayout.addWidget(self.clockWidget)
        self.settingsLayout.addWidget(self.channelTabWidget)

        # Set FFT scope to take max available space
        fftScopeSizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                                   QtWidgets.QSizePolicy.Expanding)
        self.fftScopeWidget.setSizePolicy(fftScopeSizePolicy)
        self.fftScopeWidget.setMinimumSize(400, 200)

        self.statusbar = QtWidgets.QStatusBar(self)
        self.statusbar.setObjectName("statusbar")
        self.setStatusBar(self.statusbar)

    def update_stream(self, payload):
        self.fftScopeWidget.update(payload)

    async def update_transfer_function(self, setting):
        """Update transfer function plot based on setting change."""
        if setting.root().name == "ui" and (
                ui_iir :=
                setting.get_parent_until(lambda x: x.name.startswith("iir"))) is not None:
            (_ch, _iir) = (int(ui_iir.get_parent().name[2:]), int(ui_iir.name[3:]))

            filter_type = ui_iir.get_child("filter").value
            filter = ui_iir.get_child(filter_type)

            if filter_type == "none":
                ba = get_filter("none").get_coefficients()
            else:
                filter_params = {setting.name: setting.value for setting in filter.get_children()}
                ba = get_filter(filter_type).get_coefficients(self.fftScopeWidget.sample_period, **filter_params)

            _iir_widgets = self.channels[_ch].iir_widgets[_iir]
            _iir_widgets.update_transfer_function(ba)

    # I gains in KiloHertz
    kilo = (
        lambda w: ui_mqtt_bridge.read(w) * 1e3,
        lambda w, v: ui_mqtt_bridge.write(w, v / 1e3),
    )
    # II gains in KiloHertz^2
    kilo2 = (
        lambda w: ui_mqtt_bridge.read(w) * 1e3,
        lambda w, v: ui_mqtt_bridge.write(w, v / 1e3),
    )

    # Clock in MegaHertz
    mega = (
        lambda widgets: ui_mqtt_bridge.read(widgets) * 1e6,
        lambda widgets, value: ui_mqtt_bridge.write(widgets, value / 1e6),
    )

    def _is_inf_widgets_readwrite(self):

        def read(widgets):
            """Expects widgets in the form [spinbox, checkbox]."""
            if widgets[1].isChecked():
                return inf
            else:
                return widgets[0].value()

        def write(widgets, value):
            """Expects widgets in the form [spinbox, checkbox]."""
            if value == inf:
                widgets[1].setChecked(True)
            else:
                widgets[0].setValue(value)

        return read, write

    def set_mqtt_configs(self, stream_target: NetworkAddress):
        """ Link the UI widgets to the MQTT topic tree"""

        settings_map = {}

        # `ui/#` are only used by the UI, the others by both UI and stabilizer
        settings_map[stabilizer.stream_target.get_path_from_root()] = UiMqttConfig(
            [],
            lambda _: stream_target._asdict(),
            lambda _w, _v: stream_target._asdict(),
        )

        settings_map[stabilizer.ext_clk.get_path_from_root()] = UiMqttConfig(
            [self.clockWidget.extClkCheckBox])
        settings_map[stabilizer.ref_clk_frequency.get_path_from_root()] = UiMqttConfig(
            [self.clockWidget.refFrequencyBox], *self.mega)
        settings_map[stabilizer.clk_multiplier.get_path_from_root()] = UiMqttConfig(
            [self.clockWidget.multiplierBox])

        for ch in range(NUM_CHANNELS):
            settings_map[stabilizer.afes[ch].get_path_from_root()] = UiMqttConfig(
                [self.channels[ch].afeGainBox])

            settings_map[
                stabilizer.attenuation_ins[ch].get_path_from_root()] = UiMqttConfig(
                    [self.channels[ch].ddsInAttenuationBox])
            settings_map[
                stabilizer.attenuation_outs[ch].get_path_from_root()] = UiMqttConfig(
                    [self.channels[ch].ddsOutAttenuationBox])

            settings_map[
                stabilizer.amplitude_dds_ins[ch].get_path_from_root()] = UiMqttConfig(
                    [self.channels[ch].ddsInAmplitudeBox])
            settings_map[
                stabilizer.amplitude_dds_outs[ch].get_path_from_root()] = UiMqttConfig(
                    [self.channels[ch].ddsOutAmplitudeBox])

            settings_map[
                stabilizer.frequency_dds_outs[ch].get_path_from_root()] = UiMqttConfig(
                    [self.channels[ch].ddsOutFrequencyBox], *self.mega)
            settings_map[
                stabilizer.frequency_dds_ins[ch].get_path_from_root()] = UiMqttConfig(
                    [self.channels[ch].ddsInFrequencyBox], *self.mega)
            
            settings_map[
                ui.dds_io_link_checkboxes[ch].get_path_from_root()] = UiMqttConfig(
                    [self.channels[ch].ddsIoFreqLinkCheckBox])

            # IIR settings
            for iir in range(NUM_IIR_FILTERS_PER_CHANNEL):
                iirWidget = self.channels[ch].iir_widgets[iir]

                for child in ui.iirs[ch][iir].get_children(
                    ["y_offset", "y_min", "y_max", "x_offset"]):
                    settings_map[child.get_path_from_root()] = UiMqttConfig(
                        [getattr(iirWidget, child.name + "Box")])

                settings_map[ui.iirs[ch][iir].get_child(
                    "filter").get_path_from_root()] = UiMqttConfig(
                        [iirWidget.filterComboBox])
                for filter in FILTERS:
                    filter_topic = ui.iirs[ch][iir].get_child(filter.filter_type)
                    for param in filter_topic.get_children():
                        widget_attribute = lambda suffix: getattr(
                            iirWidget.widgets[filter.filter_type], f"{param.name}{suffix}"
                        )

                        if param.name.split("_")[-1] == "limit":
                            settings_map[param.get_path_from_root()] = UiMqttConfig(
                                [
                                    widget_attribute("Box"),
                                    widget_attribute("IsInf"),
                                ],
                                *self._is_inf_widgets_readwrite(),
                            )
                        elif param.name in {"f0", "Ki"}:
                            settings_map[param.get_path_from_root()] = UiMqttConfig(
                                [widget_attribute("Box")], *self.kilo)
                        elif param.name == "Kii":
                            settings_map[param.get_path_from_root()] = UiMqttConfig(
                                [widget_attribute("Box")], *self.kilo2)
                        else:
                            settings_map[param.get_path_from_root()] = UiMqttConfig(
                                [widget_attribute("Box")])

        return settings_map
