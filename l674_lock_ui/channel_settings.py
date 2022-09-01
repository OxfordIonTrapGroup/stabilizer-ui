from PyQt5 import QtWidgets
from .ui_utils import link_slider_to_spinbox

class ChannelSettings(QtWidgets.QWidget):
    afe_options = ["G1", "G2", "G5", "G10"]

    def __init__(self):
        super().__init__()
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "channel_settings.ui")
        uic.loadUi(ui_path, self)

        for afe in [self.afe0GainBox, self.afe1GainBox]:
            afe.addItems(self.afe_options)

        self._link_paired_widgets()


    def _link_paired_widgets(self):
        pass
