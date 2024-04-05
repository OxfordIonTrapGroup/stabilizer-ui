import os
from PyQt5 import QtWidgets, uic
from math import inf
import logging

from .topics import TopicTree
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
        self.clockWidget = uic.loadUi(
            os.path.join(os.path.dirname(os.path.realpath(__file__)), "widgets/clock.ui"))

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

    def set_mqtt_configs(self, root_topic: TopicTree, stream_target: NetworkAddress):
        """ Link the UI widgets to the MQTT topic tree"""
        stabilizer_settings, ui_settings = root_topic.get_children(["settings", "ui"])

        # `ui/#` are only used by the UI, the others by both UI and stabilizer
        stabilizer_settings.get_child("stream_target").bridge_mqtt(
            UiMqttConfig(
                [],
                lambda _: stream_target._asdict(),
                lambda _w, _v: stream_target._asdict(),
            ))

        # Clock settings
        clk_multiplier, ext_clk, clk_freq = ui_settings.get_child("clock").get_children(
            ["multiplier", "extClock", "frequency"])

        clk_multiplier.bridge_mqtt(UiMqttConfig([self.clockWidget.multiplierBox]))
        clk_freq.bridge_mqtt(UiMqttConfig([self.clockWidget.frequencyBox]))
        ext_clk.bridge_mqtt(UiMqttConfig([self.clockWidget.extClkCheckBox]))

        logger.info(f"{ext_clk._ui_mqtt_config}")

        for ch_index, afe_ch in enumerate(
                stabilizer_settings.get_child("afe").get_children()):
            afe_ch.bridge_mqtt(UiMqttConfig([self.channels[ch_index].afeGainBox]))
        for (ch_index, channel) in enumerate(ui_settings.get_children(["ch0", "ch1"])):
            # Pounder settings
            dds_in, dds_out = channel.get_children(["pounder/ddsIn", "pounder/ddsOut"])

            widget_attribute = lambda dds, suffix: getattr(self.channels[ch_index],
                                                           f"{dds.name}{suffix}Box")

            for dds in (dds_in, dds_out):
                for child in dds.get_children(["attenuation", "amplitude", "frequency"]):
                    child.bridge_mqtt(
                        UiMqttConfig([widget_attribute(dds, child.name.capitalize())]))

            for (iir_index, iir) in enumerate(channel.get_children(["iir0", "iir1"])):
                iirWidget = self.channels[ch_index].iir_settings[iir_index]

                iir.get_child("filter").bridge_mqtt(
                    UiMqttConfig([iirWidget.filterComboBox]))
                iir.get_child("x_offset").bridge_mqtt(
                    UiMqttConfig([iirWidget.x_offsetBox]))
                iir.get_child("y_offset").bridge_mqtt(
                    UiMqttConfig([iirWidget.y_offsetBox]))
                iir.get_child("y_max").bridge_mqtt(UiMqttConfig([iirWidget.y_maxBox]))
                iir.get_child("y_min").bridge_mqtt(UiMqttConfig([iirWidget.y_minBox]))

                for filter in FILTERS:
                    filter_topic = iir.get_child(filter.filter_type)
                    for arg in filter_topic.get_children():
                        widget_attribute = lambda suffix: getattr(
                            iirWidget.widgets[filter.filter_type], f"{arg.name}{suffix}")

                        if arg.name.split("_")[-1] == "limit":
                            arg.bridge_mqtt(
                                UiMqttConfig(
                                    [
                                        widget_attribute("Box"),
                                        widget_attribute("IsInf"),
                                    ],
                                    *self._is_inf_widgets_readwrite(),
                                ))
                        elif arg.name in {"f0", "Ki"}:
                            arg.bridge_mqtt(
                                UiMqttConfig([widget_attribute("Box")], *self.kilo))
                        elif arg.name == "Kii":
                            arg.bridge_mqtt(
                                UiMqttConfig([widget_attribute("Box")], *self.kilo2))
                        else:
                            arg.bridge_mqtt(UiMqttConfig([widget_attribute("Box")]))
