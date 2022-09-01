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

logger = logging.getLogger(__name__)


class UI(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        self.channel_settings = [
        ('Channel 0', ChannelSettings()),
        ('Channel 1', ChannelSettings())
        ]

        tab_channel_settings = QtWidgets.QTabWidget()
        for label, channel in self.channel_settings:
            tab_channel_settings.addTab(channel, label)

        self.setCentralWidget(tab_channel_settings)


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
