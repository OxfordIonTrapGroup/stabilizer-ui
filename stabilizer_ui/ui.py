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

        self._connection_is_nominal = True
        self.stylesheet = {}

        # Add a label to the status bar to show the connection status
        self.comm_status_label = QLabel()
        self.statusBar().addPermanentWidget(self.comm_status_label)

        # Avoid the small lines to the right of every status bar item, since we
        # only have one here.
        self.statusBar().setStyleSheet("QStatusBar::item { border-width: 0px; }")

        # Message box indicating stabilizer is offline
        self._offlineMessageBox = QMessageBox()
        self._offlineMessageBox.setText("Stabilizer offline")
        self._offlineMessageBox.setInformativeText(
            "Check the stabilizer's network connection.")
        self._offlineMessageBox.setIcon(QMessageBox.Warning)
        self._offlineMessageBox.setStandardButtons(QMessageBox.Ok)
        self._offlineMessageBox.setModal(True)

        # Message box indicating Stabilizer failed to respond.
        self._commErrorMessageBox = QMessageBox()
        self._commErrorMessageBox.setText("Stabilizer failed to respond")
        self._commErrorMessageBox.setInformativeText(
            "Check the stabilizer's network connection.")
        self._commErrorMessageBox.setIcon(QMessageBox.Warning)
        self._commErrorMessageBox.setStandardButtons(QMessageBox.Ok)
        self._commErrorMessageBox.setModal(True)

        # Message box showing panic message upon stabilizer reboot after panic
        self._panicMessageBox = QMessageBox()
        self._panicMessageBox.setText("Stabilizer panicked!")
        self._panicMessageBox.setIcon(QMessageBox.Critical)
        self._panicMessageBox.setInformativeText(f"Stabilizer had panicked, but has since restarted. "\
            "You may need to change some settings if the issue persists.")
        self._panicMessageBox.setStandardButtons(QMessageBox.Ok)

    def _setStyleSheet(self):
        stylesheet_str = ";".join(
            [f"{key}: {value}" for key, value in self.stylesheet.items()])
        self.setStyleSheet(stylesheet_str)

    def update_panic_status(self, has_panicked: bool, value: Optional[str]):
        if not has_panicked:
            self.stylesheet.pop("background-color")
            self.setWindowTitle(self._windowTitle)
            return

        self._panicMessageBox.setDetailedText(f"Diagnostic information: \n{value}")
        self._panicMessageBox.open()

    def update_alive_status(self, is_alive: bool):
        if self._connection_is_nominal == is_alive:
            return
        self._connection_is_nominal = is_alive
        if not is_alive:
            self._offlineMessageBox.open()
        self._set_hardware_live_styling(is_alive)

    def update_comm_status(self, is_nominal: bool, message: str):
        self.comm_status_label.setText(message)
        if self._connection_is_nominal == is_nominal:
            return
        if not is_nominal:
            self._commErrorMessageBox.setDetailedText(message)
            self._commErrorMessageBox.show()
        self._set_hardware_live_styling(is_nominal)

    def is_dark_theme(self):
        """Guess whether the current theme is dark or light by comparing the default text and
        background colors.
        """
        text_hsv_value = self.palette().color(QPalette.WindowText).value()
        bg_hsv_value = self.palette().color(QPalette.Background).value()
        return text_hsv_value > bg_hsv_value

    def _set_hardware_live_styling(self, is_live: bool):
        if is_live:
            self.stylesheet.pop("background-color")
            self.setWindowTitle(self._windowTitle)
        else:
            bg = "maroon" if self.is_dark_theme() else "mistyrose"
            self.stylesheet["background-color"] = bg
            self._windowTitle = self.windowTitle()
            self.setWindowTitle(f"{self._windowTitle} [OFFLINE]")

        self._setStyleSheet()

    def set_mqtt_configs(self, _stream_target: NetworkAddress):
        raise NotImplementedError

    async def update_transfer_function(self, setting):
        """Update transfer function plot based on setting change."""
        if setting.app_root().name != "ui":
            return
        ui_iir = setting.get_parent_until(lambda x: x.name.startswith("iir"))
        if ui_iir is None:
            return

        ch = int(ui_iir.get_parent().name[2:])
        iir = int(ui_iir.name[3:])

        filter_type = ui_iir.child("filter").value
        filter_topic = ui_iir.child(filter_type)

        if filter_type in ["though", "block"]:
            ba = get_filter(filter_type).get_coefficients()
        else:
            filter_params = {
                setting.name: setting.value
                for setting in filter_topic.children()
            }
            ba = get_filter(filter_type).get_coefficients(
                self.fftScopeWidget.sample_period, **filter_params)

        try:
            self.channels[ch].iir_widgets[iir].update_transfer_function(ba)
        except NameError:
            logger.error("Unable to update transfer function: widget not found")
        except KeyError:
            logger.error(
                "Unable to update transfer function: incorrect number of channels")
