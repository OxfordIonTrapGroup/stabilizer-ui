import os
import numpy as np
from scipy import signal
from PyQt6 import QtWidgets, QtCore, uic
from stabilizer_ui.scientific_spinbox import ScientificSpinBox

from . import filters
from .filters import FILTERS
from ..mqtt import UiMqttConfig, combine_configs
from ..utils import link_spinbox_to_is_inf_checkbox, kilo, kilo2


#: Time for which the filter on the device has to differ from the one given by the
#: settings before a warning is shown, in seconds.
MISMATCH_DELAY = 2.0


class AbstractChannelSettings(QtWidgets.QWidget):
    """ Abstract class for creating custom channel widgets.
    Sets up AFE gains and IIR filter settings.
    """
    afe_options = ["G1", "G2", "G5", "G10"]
    run_options = ["Run", "Hold", "External"]

    def __init__(self):
        super().__init__()

    def _add_afe_options(self):
        self.afeGainBox.addItems(self.afe_options)
        self.runModeBox.addItems(self.run_options)

    def _add_iir_tabWidget(self, sample_period):
        self.iir_widgets = [_IIRWidget(sample_period), _IIRWidget(sample_period)]
        for i, iir in enumerate(self.iir_widgets):
            self.IIRTabs.addTab(iir, f"Filter {i}")

    def set_iir_available(self, index: int, available: bool):
        """Disable the tab of a filter which the firmware of the device does not have."""
        self.IIRTabs.setTabEnabled(index, available)
        self.IIRTabs.setTabToolTip(
            index, "" if available else "Not available in the firmware of the device")


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
        #: The biquad coefficients of the current settings (`idsp` convention).
        self.coefficients = None

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
                                            QtCore.Qt.ItemDataRole.ToolTipRole)

        # Warning for when the filter on the device is not the one these settings give
        # (see `set_device_mismatch()`).
        self.deviceMismatchLabel = QtWidgets.QLabel()
        self.deviceMismatchLabel.setWordWrap(True)
        self.deviceMismatchLabel.setStyleSheet("color: darkorange")
        self.writeToDeviceButton = QtWidgets.QPushButton("Write to device")
        self.writeToDeviceButton.setToolTip(
            "Replace the filter on the device by the one given by these settings")
        for widget in [self.deviceMismatchLabel, self.writeToDeviceButton]:
            self.filterParamsLayout.addWidget(widget)
            widget.hide()
        self._mismatch = None
        self._mismatch_timer = QtCore.QTimer(self)
        self._mismatch_timer.setSingleShot(True)
        self._mismatch_timer.setInterval(int(MISMATCH_DELAY * 1e3))
        self._mismatch_timer.timeout.connect(self._show_device_mismatch)

        plot = self.transferFunctionView.addPlot(row=0, col=0)
        self.widgets["transferFunctionView"] = plot

        self.frequencies = np.logspace(-8.5, 0, 1024,
                                       endpoint=False) * (0.5 / self.sample_period)
        
        # Change default precision and step size for dac settings
        for setting in ["y_offset", "y_min", "y_max", "x_offset"]:
            spinBox = getattr(self, setting + "Box")
            spinBox.setDecimals(4)
            spinBox.setSingleStep(1e-2)

        # With a log axis, there is no point in also having a power of ten taken out
        # (just leads to ticks with small values and a "(x1e06)" addition to the label).
        plot.getAxis("bottom").enableAutoSIPrefix(False)

        plot.setLogMode(True, False)
        plot.setRange(
            xRange=[np.log10(min(self.frequencies)),
                    np.log10(max(self.frequencies))],
            update=False)
        plot.setLabels(left="Magnitude (dB)", bottom="Frequency (Hz)")

        # Disable divide by zero warnings
        np.seterr(divide='ignore')

    def update_transfer_function(self, coefficients):
        self.coefficients = list(coefficients)
        # The coefficients are in the `idsp` convention,
        # `y0 = b0*x0 + b1*x1 + b2*x2 + a1*y1 + a2*y2`.
        f, h = signal.freqz(
            coefficients[:3],
            np.r_[1, [-c for c in coefficients[3:]]],
            worN=self.frequencies,
            fs=1 / self.sample_period,
        )
        # TODO: setData isn't working?
        self.widgets["transferFunctionView"].clear()
        self.widgets["transferFunctionView"].plot(f, 20 * np.log10(np.absolute(h)))

    def set_device_mismatch(self, message: str | None, can_write: bool = True):
        """Warn that the filter on the device is not the one given by these settings
        (`None` if it is), with `can_write` offering to write it to the device.

        The warning only appears once this has been the case for `MISMATCH_DELAY`, as
        the settings and the filter arrive separately when another client changes them.
        """
        self._mismatch = None if message is None else (message, can_write)
        if message is None:
            self._mismatch_timer.stop()
            self._show_device_mismatch()
        elif not self.deviceMismatchLabel.isHidden():
            self._show_device_mismatch()
        elif not self._mismatch_timer.isActive():
            self._mismatch_timer.start()

    def _show_device_mismatch(self):
        message, can_write = self._mismatch or ("", False)
        self.deviceMismatchLabel.setText(message)
        self.deviceMismatchLabel.setVisible(self._mismatch is not None)
        self.writeToDeviceButton.setVisible(can_write)

    def set_mqtt_configs(self, settings_map, iir_topic, handlers=None):
        """Bind all the settings of the filter to `iir_topic`, as one value with the
        filter type (`filter`), the offsets and limits, and the parameters of each filter
        type (by its name, e.g. `pid/Kp`).

        `handlers` replaces the read and write handlers of parameters, by their path.
        """
        parts = {
            name: UiMqttConfig([getattr(self, name + "Box")])
            for name in ["y_offset", "y_min", "y_max", "x_offset"]
        }
        parts["filter"] = UiMqttConfig([self.filterComboBox])

        for filter in FILTERS:
            widget = self.widgets[filter.filter_type]
            for param in filter.parameters:
                box = getattr(widget, f"{param}Box")
                if param.split("_")[-1] == "limit":
                    cfg = UiMqttConfig([box, getattr(widget, f"{param}IsInf")],
                                       *link_spinbox_to_is_inf_checkbox())
                elif param in {"f0", "Ki"}:
                    cfg = UiMqttConfig([box], *kilo)
                elif param == "Kii":
                    cfg = UiMqttConfig([box], *kilo2)
                else:
                    cfg = UiMqttConfig([box])
                parts[f"{filter.filter_type}/{param}"] = cfg

        for path, handler in (handlers or {}).items():
            parts[path] = UiMqttConfig(parts[path].widgets, *handler)

        settings_map[iir_topic.path()] = combine_configs(parts)


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
