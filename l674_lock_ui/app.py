import asyncio
import os
from PyQt5 import QtWidgets, uic
from qasync import QEventLoop
import sys

from .ui_utils import link_slider_to_spinbox


class UI(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "app.ui")
        uic.loadUi(ui_path, self)

        self._link_paired_widgets()

    def _link_paired_widgets(self):
        for s, b in [(self.fastPGainSlider, self.fastPGainBox),
                     (self.fastIGainSlider, self.fastIGainBox),
                     (self.notchFreqSlider, self.notchFreqBox),
                     (self.notchQSlider, self.notchQBox),
                     (self.slowPGainSlider, self.slowPGainBox),
                     (self.slowIGainSlider, self.slowIGainBox)]:
            link_slider_to_spinbox(s, b)


def main():
    app = QtWidgets.QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    ui = UI()
    ui.show()
    app.exec_()


if __name__ == "__main__":
    main()
