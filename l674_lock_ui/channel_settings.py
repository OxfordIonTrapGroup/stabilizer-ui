from PyQt5 import QtCore, QtWidgets, uic
import os
from stabilizer.iir_coefficients import get_filters

from .ui_utils import link_slider_to_spinbox

class ChannelSettings(QtWidgets.QWidget):
    afe_options = ["G1", "G2", "G5", "G10"]

    def __init__(self):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "channel_settings.ui")
        uic.loadUi(ui_path, self)

        self.afeGainBox.addItems(self.afe_options)

        self.iir_settings = [
        ('Filter 1', _IIRWidget()),
        ('Filter 2', _IIRWidget())
        ]
        for label, iir in self.iir_settings:
            self.IIRTabs.addTab(iir, label)

class _IIRWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "widgets/iir.ui")
        uic.loadUi(ui_path, self)

        self.filters = get_filters() # Obtains dict of filters from stabilizer.py module
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
