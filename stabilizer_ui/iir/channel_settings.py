import os
import numpy as np
from scipy import signal
from PyQt5 import QtWidgets, QtCore, uic
from artiq.gui.scientific_spinbox import ScientificSpinBox

from . import filters
from .filters import FILTERS
from ..mqtt import UiMqttConfig
from ..utils import link_spinbox_to_is_inf_checkbox, kilo, kilo2


class AbstractChannelSettings(QtWidgets.QWidget):
    """ Abstract class for creating custom channel widgets.
    Sets up AFE gains and IIR filter settings.
    """
    afe_options = ["G1", "G2", "G5", "G10"]

    def __init__(self):
        super().__init__()

    def _add_afe_options(self):
        self.afeGainBox.addItems(self.afe_options)

    def _add_iir_tabWidget(self, sample_period):
        self.iir_widgets = [_IIRWidget(sample_period), _IIRWidget(sample_period)]
        for i, iir in enumerate(self.iir_widgets):
            self.IIRTabs.addTab(iir, f"Filter {i}")


class ChannelSettings(AbstractChannelSettings):
    """ Minimal channel settings widget for a dual-iir-like application
    """

    def __init__(self, sample_period):
        super().__init__()

        uic.loadUi(
            os.path.join(os.path.dirname(os.path.realpath(__file__)),
                         "widgets/channel_settings.ui"), self)

        self._add_afe_options()
        self._add_iir_tabWidget(sample_period)


class _IIRWidget(QtWidgets.QWidget):

    def __init__(self, sample_period):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "widgets/iir.ui")
        uic.loadUi(ui_path, self)

        self.sample_period = sample_period

        # Obtains dict of filters from stabilizer.py module
        self.filters = (filters.filters())
        self.widgets = {}

        # Add filter parameter widgets to filterParamsStack
        for _filter in self.filters.keys():
            self.filterComboBox.addItem(_filter)
            if _filter == "pid":
                _widget = _PIDWidget()
                _tooltip = "PID"
            elif _filter == "notch":
                _widget = _NotchWidget()
                _tooltip = "Notch filter"
            elif _filter in ["lowpass", "highpass", "allpass"]:
                _widget = _XPassWidget()
                _tooltip = f"{_filter.capitalize()} filter"
            elif _filter == "through":
                _widget = QtWidgets.QWidget()
                _tooltip = "Passes the input through unfiltered"
            elif _filter == "block":
                _widget = QtWidgets.QWidget()
                _tooltip = "Blocks the input via a digital filter"
            else:
                raise ValueError()

            self.widgets[_filter] = _widget
            self.filterParamsStack.addWidget(_widget)
            self.filterComboBox.setItemData(self.filterComboBox.count() - 1, _tooltip,
                                            QtCore.Qt.ToolTipRole)

        self.widgets["transferFunctionView"] = self.transferFunctionView.addPlot(row=0,
                                                                                 col=0)

        self.frequencies = np.logspace(-8.5, 0, 1024,
                                       endpoint=False) * (0.5 / self.sample_period)
        plot_config = {
            "ylabel": "Magnitude (dB)",
            "xlabel": "Frequency (Hz)",
            "log": [True, False],
            "xrange": [np.log10(min(self.frequencies)),
                       np.log10(max(self.frequencies))],
        }

        self.widgets["transferFunctionView"].setLogMode(*plot_config["log"])
        self.widgets["transferFunctionView"].setRange(xRange=plot_config["xrange"],
                                                      update=False)
        self.widgets["transferFunctionView"].setLabels(left=plot_config["ylabel"],
                                                       bottom=plot_config["xlabel"])

        # Disable divide by zero warnings
        np.seterr(divide='ignore')

    def update_transfer_function(self, coefficients):
        f, h = signal.freqz(
            coefficients[:3],
            np.r_[1, [c for c in coefficients[3:]]],
            # TODO: Simplfy once the stabilizer python script is updated
            worN=self.frequencies,
            fs=1 / self.sample_period,
        )
        # TODO: setData isn't working?
        self.widgets["transferFunctionView"].clear()
        self.widgets["transferFunctionView"].plot(f, 20 * np.log10(np.absolute(h)))

    def set_mqtt_configs(self, settings_map, iir_topic):
        for child in iir_topic.children(["y_offset", "y_min", "y_max", "x_offset"]):
            settings_map[child.path()] = UiMqttConfig([getattr(self, child.name + "Box")])

        settings_map[iir_topic.child("filter").path()] = UiMqttConfig(
            [self.filterComboBox])

        for filter in FILTERS:
            filter_topic = iir_topic.child(filter.filter_type)
            for param in filter_topic.children():
                widget_attribute = lambda suffix: getattr(
                    self.widgets[filter.filter_type], f"{param.name}{suffix}")

                if param.name.split("_")[-1] == "limit":
                    settings_map[param.path()] = UiMqttConfig(
                        [
                            widget_attribute("Box"),
                            widget_attribute("IsInf"),
                        ],
                        *link_spinbox_to_is_inf_checkbox(),
                    )
                elif param.name in {"f0", "Ki"}:
                    settings_map[param.path()] = UiMqttConfig([widget_attribute("Box")],
                                                              *kilo)
                elif param.name == "Kii":
                    settings_map[param.path()] = UiMqttConfig([widget_attribute("Box")],
                                                              *kilo2)
                else:
                    settings_map[param.path()] = UiMqttConfig([widget_attribute("Box")])


class _PIDWidget(QtWidgets.QWidget):

    def __init__(self):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "widgets/pid_settings.ui")
        uic.loadUi(ui_path, self)

        for spinbox in self.findChildren(ScientificSpinBox):
            spinbox.setSigFigs(3)


class _NotchWidget(QtWidgets.QWidget):

    def __init__(self):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "widgets/notch_settings.ui")
        uic.loadUi(ui_path, self)


class _XPassWidget(QtWidgets.QWidget):

    def __init__(self):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "widgets/xpass_settings.ui")
        uic.loadUi(ui_path, self)
