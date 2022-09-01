import argparse
import asyncio
import logging
import numpy as np
import numpy.fft as fft
import os
from PyQt5 import QtGui, QtWidgets, uic
from qasync import QEventLoop
import sys

from ...channel_settings import ChannelSettings
from ...stream.fft_scope import FftScope

logger = logging.getLogger(__name__)


class UI(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        wid = QtWidgets.QWidget(self)
        self.setCentralWidget(wid)
        layout = QtWidgets.QHBoxLayout()

        # Create UI for channel settings.
        self.channel_settings = [
        ('Channel 0', ChannelSettings()),
        ('Channel 1', ChannelSettings())
        ]

        self.tab_channel_settings = QtWidgets.QTabWidget()
        for label, channel in self.channel_settings:
            self.tab_channel_settings.addTab(channel, label)
        layout.addWidget(self.tab_channel_settings)

        # Create UI for FFT scope.
        self.fft_scope = FftScope()
        layout.addWidget(self.fft_scope)

        # Set main window layout
        wid.setLayout(layout)


def main():
    logging.basicConfig(level=logging.INFO)

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName("Dual IIR UI")


    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)

        ui = UI()
        ui.resize(1200, 600)
        ui.show()

        sys.exit(loop.run_forever())


if __name__ == "__main__":
    main()
