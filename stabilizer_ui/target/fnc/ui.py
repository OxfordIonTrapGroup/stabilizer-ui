import logging
from PyQt5 import QtWidgets
from math import inf

from . import topics
from .clock import ClockWidget
from .channels import ChannelTabWidget

from ...stream.fft_scope import FftScope
from ...ui_mqtt_bridge import UiMqttConfig, NetworkAddress
from ... import ui_mqtt_bridge
from ...iir.filters import FILTERS

logger = logging.getLogger(__name__)


class MainWindow(QtWidgets.QMainWindow):
    """ Main UI window for FNC"""

    def __init__(self):
        super().__init__()

        # Set up main window with Horizontal layout
        self.setCentralWidget(QtWidgets.QWidget(self))
        self.centralWidget = self.centralWidget()
        self.centralWidgetLayout = QtWidgets.QHBoxLayout(self.centralWidget)

        # Add FFT scope next to a vertical layout for settings
        self.settingsLayout = QtWidgets.QVBoxLayout()
        self.fftScopeWidget = FftScope()
        self.centralWidgetLayout.addLayout(self.settingsLayout)
        self.centralWidgetLayout.addWidget(self.fftScopeWidget)

        self.channelTabWidget = ChannelTabWidget()
        self.channels = self.channelTabWidget.channels
        self.clockWidget = ClockWidget()

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
        self.streamWidget.update(payload)

    def update_transfer_function(self, setting):
        """Update transfer function plot based on setting change."""
        if setting.root().name == "ui" and (
                ui_iir :=
                setting.get_parent_until(lambda x: x.name.startswith("iir"))) is not None:
            (_ch, _iir) = (int(ui_iir.get_parent().name[2:]), int(ui_iir.name[3:]))

            filter_type = ui_iir.get_child("filter").value
            filter = ui_iir.get_child(filter_type)

            # require parameters to be set by application
            if not filter.has_children():
                raise ValueError(f"Filter {filter_type} parameter messsages not created.")
            filter_params = {f.name: f.value for f in filter.get_children()}

            ba = next(f for f in FILTERS
                      if f.filter_type == filter_type).get_coefficients(**filter_params)
            _iir_settings = self.channels[_ch].iir_settings[_iir]
            _iir_settings.update_transfer_function(ba)

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
        # stabilizer_settings, ui_settings = root_topic.get_children(["settings", "ui"])

        # `ui/#` are only used by the UI, the others by both UI and stabilizer
        topics.Stabilizer.stream_target.mqtt_config = UiMqttConfig(
            [],
            lambda _: stream_target._asdict(),
            lambda _w, _v: stream_target._asdict(),
        )

        topics.Stabilizer.ext_clk.mqtt_config = UiMqttConfig(
            [self.clockWidget.extClkCheckBox])
        topics.Stabilizer.ref_clk_frequency.mqtt_config = UiMqttConfig(
            [self.clockWidget.refFrequencyBox])
        topics.Stabilizer.clk_multiplier.mqtt_config = UiMqttConfig(
            [self.clockWidget.multiplierBox])

        for ch_index, (afe_ch, pounder_ch, filter_ch) in enumerate(
                zip(topics.Stabilizer.afes, topics.Stabilizer.pounder_channels,
                    topics.Ui.channels)):
            # AFE settings
            afe_ch.mqtt_config = UiMqttConfig([self.channels[ch_index].afeGainBox])

            # Pounder settings
            pounder_ch.get_child("attenuation_in").mqtt_config = UiMqttConfig(
                [self.channels[ch_index].ddsInAttenuationBox])
            pounder_ch.get_child("attenuation_out").mqtt_config = UiMqttConfig(
                [self.channels[ch_index].ddsOutAttenuationBox])

            pounder_ch.get_child("amplitude_dds_in").mqtt_config = UiMqttConfig(
                [self.channels[ch_index].ddsInAmplitudeBox])
            pounder_ch.get_child("amplitude_dds_out").mqtt_config = UiMqttConfig(
                [self.channels[ch_index].ddsOutAmplitudeBox])

            pounder_ch.get_child("frequency_dds_out").mqtt_config = UiMqttConfig(
                [self.channels[ch_index].ddsOutFrequencyBox])
            pounder_ch.get_child("frequency_dds_in").mqtt_config = UiMqttConfig(
                [self.channels[ch_index].ddsInFrequencyBox])

            # IIR settings
            for (iir_index, iir) in enumerate(filter_ch.get_children(["iir0", "iir1"])):
                iirWidget = self.channels[ch_index].iir_settings[iir_index]

                iir.get_child("filter").mqtt_config = UiMqttConfig(
                    [iirWidget.filterComboBox])
                iir.get_child("x_offset").mqtt_config = UiMqttConfig(
                    [iirWidget.x_offsetBox])
                iir.get_child("y_offset").mqtt_config = UiMqttConfig(
                    [iirWidget.y_offsetBox])
                iir.get_child("y_max").mqtt_config = UiMqttConfig([iirWidget.y_maxBox])
                iir.get_child("y_min").mqtt_config = UiMqttConfig([iirWidget.y_minBox])

                for filter in FILTERS:
                    filter_topic = iir.get_child(filter.filter_type)
                    for arg in filter_topic.get_children():
                        widget_attribute = lambda suffix: getattr(
                            iirWidget.widgets[filter.filter_type], f"{arg.name}{suffix}")

                        if arg.name.split("_")[-1] == "limit":
                            arg.mqtt_config = UiMqttConfig(
                                [
                                    widget_attribute("Box"),
                                    widget_attribute("IsInf"),
                                ],
                                *self._is_inf_widgets_readwrite(),
                            )
                        elif arg.name in {"f0", "Ki"}:
                            arg.mqtt_config = UiMqttConfig([widget_attribute("Box")],
                                                           *self.kilo)
                        elif arg.name == "Kii":
                            arg.mqtt_config = UiMqttConfig([widget_attribute("Box")],
                                                           *self.kilo2)
                        else:
                            arg.mqtt_config = UiMqttConfig([widget_attribute("Box")])
