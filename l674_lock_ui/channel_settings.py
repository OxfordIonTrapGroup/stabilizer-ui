from PyQt5 import QtWidgets, uic
import os

from .ui_utils import link_slider_to_spinbox

class ChannelSettings(QtWidgets.QWidget):
    afe_options = ["G1", "G2", "G5", "G10"]

    def __init__(self):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "channel_settings.ui")
        uic.loadUi(ui_path, self)

        self.afeGainBox.addItems(self.afe_options)

        self._link_paired_widgets()

    def _link_paired_widgets(self):
        for s, b in [(self.pGainSlider, self.pGainSpinBox),
                     (self.iGainSlider, self.iGainSpinBox),
                     (self.dGainSlider, self.dGainSpinBox),
                     (self.lp_f0Slider, self.lp_f0SpinBox),
                     (self.lp_KSlider, self.lp_KSpinBox),
                     (self.hp_f0Slider, self.hp_f0SpinBox),
                     (self.hp_KSlider, self.hp_KSpinBox),
                     (self.ap_f0Slider, self.ap_f0SpinBox),
                     (self.ap_KSlider, self.ap_KSpinBox),
                     (self.notch_f0Slider, self.notch_f0SpinBox),
                     (self.notch_QSlider, self.notch_QSpinBox),
                     (self.notch_KSlider, self.notch_KSpinBox),
                    ]:
            link_slider_to_spinbox(s, b)

