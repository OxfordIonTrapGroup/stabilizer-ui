from __future__ import annotations

import logging
from PyQt5.QtWidgets import QMainWindow, QMessageBox, QLabel, QStatusBar
from PyQt5.QtGui import QPalette
from typing import Optional

from .iir.filters import get_filter

logger = logging.getLogger(__name__)

class AbstractUiWindow(QMainWindow):
    """Abstract class for main UI window"""

    def __init__(self):
        super().__init__()

        self.stabilizerOnline = True
        self.stylesheet = {}

        self.statusbar = QStatusBar(self)
        self.statusbar.setObjectName("statusbar")
        self.setStatusBar(self.statusbar)

        # Add a label to the status bar to show the connection status
        self.comm_status_label = QLabel()
        self.statusbar.addPermanentWidget(self.comm_status_label)

        # Message box indicating stabilizer is offline
        self._offlineMessageBox = QMessageBox()
        self._offlineMessageBox.setText("Stabilizer offline")
        self._offlineMessageBox.setInformativeText("Check the stabilizer's network connection.")
        self._offlineMessageBox.setIcon(QMessageBox.Warning)
        self._offlineMessageBox.setStandardButtons(QMessageBox.Ok)
        self._offlineMessageBox.setModal(True)

        # Message box showing panic message upon stabilizer reboot after panic
        self._panicMessageBox = QMessageBox()
        self._panicMessageBox.setText("Stabilizer panicked!")
        self._panicMessageBox.setIcon(QMessageBox.Critical)
        self._panicMessageBox.setInformativeText(f"Stabilizer had panicked, but has since restarted. "\
            "You may need to change some settings if the issue persists.")
        self._panicMessageBox.setStandardButtons(QMessageBox.Ok)

    def set_comm_status(self, status: str):
        self.comm_status_label.setText(status)

    def _setStyleSheet(self):
        stylesheet_str = ";".join(
            [f"{key}: {value}" for key, value in self.stylesheet.items()])
        self.setStyleSheet(stylesheet_str)

    def onPanicStatusChange(self, isPanicked: bool, value: Optional[str]):
        if not isPanicked:
            self.stylesheet.pop("background-color")
            self.setWindowTitle(self._windowTitle)
            return

        self._panicMessageBox.setDetailedText(f"Diagnostic information: \n{value}")
        self._panicMessageBox.open()

    def is_dark_theme(self):
        r""" Guess whether the current theme is dark or light by comparing
            the text and background color of a virtual label.
            :return: True if the theme is dark, False otherwise.

        """
        # Virtual label that is deleted after use
        label = QLabel("am I in the dark?")
        text_hsv_value = label.palette().color(QPalette.WindowText).value()
        bg_hsv_value = label.palette().color(QPalette.Background).value()
        return text_hsv_value > bg_hsv_value

    def onlineStatusChange(self, isOnline: bool):
        if self.stabilizerOnline == isOnline:
            return

        self.stabilizerOnline = isOnline

        if self.stabilizerOnline:
            self.stylesheet.pop("background-color")
            self.setWindowTitle(self._windowTitle)
        else:
            self.stylesheet["background-color"] = "maroon" if self.is_dark_theme() else "mistyrose"
            self._offlineMessageBox.open()

            self._windowTitle = self.windowTitle()
            self.setWindowTitle(f"{self._windowTitle} [OFFLINE]")

        self._setStyleSheet()

    def set_mqtt_configs(self, _stream_target: NetworkAddress):
        raise NotImplementedError
    
    async def update_transfer_function(self, setting):
        """Update transfer function plot based on setting change."""
        if setting.app_root().name == "ui" and (
                ui_iir :=
                setting.get_parent_until(lambda x: x.name.startswith("iir"))) is not None:
            (_ch, _iir) = (int(ui_iir.get_parent().name[2:]), int(ui_iir.name[3:]))

            filter_type = ui_iir.child("filter").value
            filter_topic = ui_iir.child(filter_type)

            if filter_type in ["though", "block"]:
                ba = get_filter(filter_type).get_coefficients()
            else:
                filter_params = {setting.name: setting.value for setting in filter_topic.children()}
                ba = get_filter(filter_type).get_coefficients(self.fftScopeWidget.sample_period, **filter_params)

            try:
                _iir_widgets = self.channels[_ch].iir_widgets[_iir]
                _iir_widgets.update_transfer_function(ba)
            except NameError:
                logger.error("Unable to update transfer function: widget not found")
            except KeyError:
                logger.error("Unable to update transfer function: incorrect number of channels")
