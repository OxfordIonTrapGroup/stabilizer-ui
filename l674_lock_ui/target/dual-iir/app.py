import argparse
import asyncio
import logging
import numpy as np
import numpy.fft as fft
import os
from PyQt5 import QtGui, QtWidgets, uic
from qasync import QEventLoop
import sys

from ...mqtt import MqttInterface
from ...stream.fft_scope import FftScope
from ...stream.thread import StreamThread
from ...ui_utils import link_slider_to_spinbox

logger = logging.getLogger(__name__)

class ScopeUI(QtWidgets.QtWidget):
    def __init__(self):
        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "scope.ui")
        uic.loadUi(ui_path, self)

class UI(QtWidgets.QMainWindow):
    afe_options = ["G1", "G2", "G5", "G10"]

    def __init__(self):
        super().__init__()

        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "app.ui")
        uic.loadUi(ui_path, self)

        self._link_paired_widgets()

        for afe in [self.afe0GainBox, self.afe1GainBox]:
            afe.addItems(self.afe_options)

        self.scope = ScopeUI(self)


    def _link_paired_widgets(self):
        pass

def main():
    logging.basicConfig(level=logging.INFO)

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setApplicationName("Test")

    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)

        ui = UI()
        ui.show()

        sys.exit(loop.run_forever())


if __name__ == "__main__":
    main()
