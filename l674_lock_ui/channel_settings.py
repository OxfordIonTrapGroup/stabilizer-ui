from PyQt5 import QtCore, QtWidgets, uic
import os
from stabilizer.iir_coefficients import get_filters

import numpy as np
from scipy import signal

from .ui_utils import QCursorSpinBox

from stabilizer import SAMPLE_PERIOD


class ChannelSettings(QtWidgets.QWidget):
    afe_options = ["G1", "G2", "G5", "G10"]

    def __init__(self):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "channel_settings.ui")
        uic.loadUi(ui_path, self)

        self.afeGainBox.addItems(self.afe_options)

        self.iir_settings = [_IIRWidget(), _IIRWidget()]
        for i, iir in enumerate(self.iir_settings):
            self.IIRTabs.addTab(iir, f"Filter {i}")


class _IIRWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "widgets/iir.ui")
        uic.loadUi(ui_path, self)

        self.filters = get_filters(
        )  # Obtains dict of filters from stabilizer.py module
        self.widgets = {}

        # Add filter parameter widgets to filterParamsStack
        for _filter in self.filters.keys():
            self.filterComboBox.addItem(_filter)
            if _filter == 'pid':
                _widget = _PIDWidget()
            elif _filter == 'notch':
                _widget = _NotchWidget()
            elif _filter in ['lowpass', 'highpass', 'allpass']:
                _widget = _XPassWidget()
            else:
                raise ValueError()
            self.widgets[_filter] = _widget
            self.filterParamsStack.addWidget(_widget)

        self.widgets['transferFunctionView'] = self.transferFunctionView.addPlot(row=0,
                                                                                 col=0)

        self.f = np.logspace(-8.5, 0, 1024, endpoint=False) * (.5 / SAMPLE_PERIOD)
        plot_config = {
            "ylabel": "Magnitude (dB)",
            "xlabel": "Frequency (Hz)",
            "log": [True, False],
            "xrange": [np.log10(min(self.f)),
                       np.log10(max(self.f))],
        }

        self.widgets['transferFunctionView'].setLogMode(*plot_config['log'])
        self.widgets['transferFunctionView'].setRange(xRange=plot_config['xrange'],
                                                      update=False)
        self.widgets['transferFunctionView'].setLabels(left=plot_config['ylabel'],
                                                       bottom=plot_config['xlabel'])

    def update_transfer_function(self, coefficients):
        f, h = signal.freqz(
            coefficients[:3],
            np.r_[1, [-c for c in coefficients[3:]]],
            worN=self.f,
            fs=1 / SAMPLE_PERIOD,
        )
        # TODO: setData isn't working?
        self.widgets['transferFunctionView'].clear()
        self.widgets['transferFunctionView'].plot(f, 20 * np.log10(np.absolute(h)))


class _PIDWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "widgets/pid_settings.ui")
        uic.loadUi(ui_path, self)


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
