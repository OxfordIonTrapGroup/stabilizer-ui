import logging
from PyQt5 import QtWidgets
from math import inf

from .topics import stabilizer, ui
from .widgets.clock import ClockWidget
from .widgets.channels import ChannelTabWidget
from .parameters import *

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

        settings_map[stabilizer.stream_target] = UiMqttConfig(
            [],
            lambda _: stream_target._asdict(),
            lambda _w, _v: stream_target._asdict(),
        )
        # `ui/#` are only used by the UI, the others by both UI and stabilizer
        settings_map[stabilizer.stream_target] = UiMqttConfig(
            [],
            lambda _: stream_target._asdict(),
            lambda _w, _v: stream_target._asdict(),
        )

        settings_map[stabilizer.ext_clk] = UiMqttConfig([self.clockWidget.extClkCheckBox])
        settings_map[stabilizer.ref_clk_frequency] = UiMqttConfig(
            [self.clockWidget.refFrequencyBox], *self.mega)
        settings_map[stabilizer.clk_multiplier] = UiMqttConfig(
            [self.clockWidget.multiplierBox])

        for ch in range(NUM_CHANNELS):
            settings_map[stabilizer.afes[ch]] = UiMqttConfig(
                [self.channels[ch].afeGainBox])

            settings_map[stabilizer.attenuation_ins[ch]] = UiMqttConfig(
                [self.channels[ch].ddsInAttenuationBox])
            settings_map[stabilizer.attenuation_outs[ch]] = UiMqttConfig(
                [self.channels[ch].ddsOutAttenuationBox])

            settings_map[stabilizer.amplitude_dds_ins[ch]] = UiMqttConfig(
                [self.channels[ch].ddsInAmplitudeBox])
            settings_map[stabilizer.amplitude_dds_outs[ch]] = UiMqttConfig(
                [self.channels[ch].ddsOutAmplitudeBox])

            settings_map[stabilizer.frequency_dds_outs[ch]] = UiMqttConfig(
                [self.channels[ch].ddsOutFrequencyBox])
            settings_map[stabilizer.frequency_dds_ins[ch]] = UiMqttConfig(
                [self.channels[ch].ddsInFrequencyBox])

            # IIR settings
            for iir in range(NUM_IIR_FILTERS_PER_CHANNEL):
                iirWidget = lambda: self.channels[ch].iir_settings[iir]

                topic = lambda name: getattr(ui, name)[ch][iir]
                # ui_topic = lambda name: getattr(ui, name)[ch][iir]

                settings_map[topic("filters")] = UiMqttConfig([iirWidget().filterComboBox])
                settings_map[topic("x_offsets")] = UiMqttConfig([iirWidget().x_offsetBox])
                settings_map[topic("y_offsets")] = UiMqttConfig([iirWidget().y_offsetBox])
                settings_map[topic("y_maxs")] = UiMqttConfig([iirWidget().y_maxBox])
                settings_map[topic("y_mins")] = UiMqttConfig([iirWidget().y_minBox])

                for filter in FILTERS:
                    filter_topic = topic(f"{filter.filter_type}s")

                    for param in filter.parameters:
                        filter_attribute = getattr(filter_topic, param)

                        widget_attribute = lambda suffix: getattr(
                            iirWidget().widgets[filter.filter_type], f"{filter_attribute.name}{suffix}"
                        )

                        if filter_attribute.name.split("_")[-1] == "limit":
                            settings_map[filter_attribute] = UiMqttConfig(
                                [
                                    widget_attribute("Box"),
                                    widget_attribute("IsInf"),
                                ],
                                *self._is_inf_widgets_readwrite(),
                            )
                        elif filter_attribute.name in {"f0", "Ki"}:
                            settings_map[filter_attribute] = UiMqttConfig(
                                [widget_attribute("Box")], *self.kilo)
                        elif filter_attribute.name == "Kii":
                            settings_map[filter_attribute] = UiMqttConfig(
                                [widget_attribute("Box")], *self.kilo2)
                        else:
                            settings_map[filter_attribute] = UiMqttConfig(
                                [widget_attribute("Box")])

        # for ch_index, (afe_ch, pounder_ch, filter_ch) in enumerate(
        #         zip(stabilizer.afes, stabilizer.pounder_channels,
        #             topics.ui.channels)):
        #     # AFE settings
        #     settings_map[afe_ch] = UiMqttConfig([self.channels[ch_index].afeGainBox])

        #     # Pounder settings
        #     settings_map[pounder_ch.get_child("attenuation_in")] = UiMqttConfig(
        #         [self.channels[ch_index].ddsInAttenuationBox])
        #     settings_map[pounder_ch.get_child("attenuation_out")] = UiMqttConfig(
        #         [self.channels[ch_index].ddsOutAttenuationBox])

        #     settings_map[pounder_ch.get_child("amplitude_dds_in")] = UiMqttConfig(
        #         [self.channels[ch_index].ddsInAmplitudeBox])
        #     settings_map[pounder_ch.get_child("amplitude_dds_out")] = UiMqttConfig(
        #         [self.channels[ch_index].ddsOutAmplitudeBox])

        #     settings_map[pounder_ch.get_child("frequency_dds_out")] = UiMqttConfig(
        #         [self.channels[ch_index].ddsOutFrequencyBox])
        #     settings_map[pounder_ch.get_child("frequency_dds_in")] = UiMqttConfig(
        #         [self.channels[ch_index].ddsInFrequencyBox])

        # IIR settings
        # for (iir_index, iir) in enumerate(filter_ch.get_children(["iir0", "iir1"])):
        #     iirWidget = self.channels[ch_index].iir_settings[iir_index]

        #     settings_map[iir.get_child("filter")] = UiMqttConfig(
        #         [iirWidget.filterComboBox])
        #     settings_map[iir.get_child("x_offset")] = UiMqttConfig(
        #         [iirWidget.x_offsetBox])
        #     settings_map[iir.get_child("y_offset")] = UiMqttConfig(
        #         [iirWidget.y_offsetBox])
        #     settings_map[iir.get_child("y_max")] = UiMqttConfig([iirWidget.y_maxBox])
        #     settings_map[iir.get_child("y_min")] = UiMqttConfig([iirWidget.y_minBox])

        #     for filter in FILTERS:
        #         filter_topic = iir.get_child(filter.filter_type)
        #         for arg in filter_topic.get_children():
        #             widget_attribute = lambda suffix: getattr(
        #                 iirWidget.widgets[filter.filter_type], f"{arg.name}{suffix}")

        #             if arg.name.split("_")[-1] == "limit":
        #                 settings_map[arg] = UiMqttConfig(
        #                     [
        #                         widget_attribute("Box"),
        #                         widget_attribute("IsInf"),
        #                     ],
        #                     *self._is_inf_widgets_readwrite(),
        #                 )
        #             elif arg.name in {"f0", "Ki"}:
        #                 settings_map[arg] = UiMqttConfig([widget_attribute("Box")],
        #                                                  *self.kilo)
        #             elif arg.name == "Kii":
        #                 settings_map[arg] = UiMqttConfig([widget_attribute("Box")],
        #                                                  *self.kilo2)
        #             else:
        #                 settings_map[arg] = UiMqttConfig([widget_attribute("Box")])

        return settings_map
