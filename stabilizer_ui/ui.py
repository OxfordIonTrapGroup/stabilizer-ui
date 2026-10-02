from __future__ import annotations

import logging
from PyQt6.QtWidgets import (QMainWindow, QDialog, QMenu, QMessageBox, QLabel,
                             QPushButton)
from PyQt6.QtGui import QPalette
from typing import Optional

from .iir.filters import settings_coefficients

logger = logging.getLogger(__name__)


class AbstractUiWindow(QMainWindow):
    """Abstract class for main UI window

    Subclasses are expected to have the widgets for the device settings in
    `channelTabWidget`, and the scope as `fftScopeWidget`.
    """

    def __init__(self):
        super().__init__()

        self._connection_is_nominal = True
        self.stylesheet = {}
        self._tools_menu = None
        #: Whether the device settings are confirmed, see `set_settings_enabled()`.
        self._settings_enabled = False

        # Shown in the status bar while the stream is not directed to this client
        self.stream_status_label = QLabel()
        self.stream_status_label.setStyleSheet("color: darkorange")
        self.streamTakeOverButton = QPushButton("Take over stream")
        self.streamTakeOverButton.setToolTip(
            "Direct the data stream of the device to this window. It can only go to "
            "one client at a time.")
        for widget in [self.stream_status_label, self.streamTakeOverButton]:
            self.statusBar().addPermanentWidget(widget)
            widget.hide()

        # Add a label to the status bar to show the connection status
        self.comm_status_label = QLabel()
        self.statusBar().addPermanentWidget(self.comm_status_label)

        # Avoid the small lines to the right of every status bar item, since we
        # only have one here.
        self.statusBar().setStyleSheet("QStatusBar::item { border-width: 0px; }")

        # Start disabled, not just once the MQTT task first runs, to avoid a flash of
        # enabled widgets.
        self.set_settings_enabled(False)

        # Message box indicating stabilizer is offline
        self._offlineMessageBox = QMessageBox()
        self._offlineMessageBox.setText("Stabilizer offline")
        self._offlineMessageBox.setInformativeText(
            "Check the stabilizer's network connection.")
        self._offlineMessageBox.setIcon(QMessageBox.Icon.Warning)
        self._offlineMessageBox.setStandardButtons(QMessageBox.StandardButton.Ok)
        self._offlineMessageBox.setModal(True)

        # Message box indicating Stabilizer failed to respond.
        self._commErrorMessageBox = QMessageBox()
        self._commErrorMessageBox.setText("Stabilizer failed to respond")
        self._commErrorMessageBox.setInformativeText(
            "Check the stabilizer's network connection.")
        self._commErrorMessageBox.setIcon(QMessageBox.Icon.Warning)
        self._commErrorMessageBox.setStandardButtons(QMessageBox.StandardButton.Ok)
        self._commErrorMessageBox.setModal(True)

        # Message box showing panic message upon stabilizer reboot after panic
        self._panicMessageBox = QMessageBox()
        self._panicMessageBox.setText("Stabilizer panicked!")
        self._panicMessageBox.setIcon(QMessageBox.Icon.Critical)
        self._panicMessageBox.setInformativeText(
            "Stabilizer had panicked, but has since restarted. " +
            "You may need to change some settings if the issue persists.")
        self._panicMessageBox.setStandardButtons(QMessageBox.StandardButton.Ok)

    def tools_menu(self) -> QMenu:
        """The Tools menu (added on first use)."""
        if self._tools_menu is None:
            self._tools_menu = self.menuBar().addMenu("&Tools")
        return self._tools_menu

    def _setStyleSheet(self):
        stylesheet_str = ";".join(
            [f"{key}: {value}" for key, value in self.stylesheet.items()])
        self.setStyleSheet(stylesheet_str)

    def update_panic_status(self, has_panicked: bool, value: Optional[str]):
        if not has_panicked:
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
        if is_nominal:
            self._commErrorMessageBox.hide()
            self._offlineMessageBox.hide()
        if self._connection_is_nominal == is_nominal:
            return
        self._connection_is_nominal = is_nominal
        if not is_nominal:
            self._commErrorMessageBox.setDetailedText(message)
            self._commErrorMessageBox.show()
        self._set_hardware_live_styling(is_nominal)

    def update_stream_status(self, message: Optional[str]):
        """Show why the stream is not directed to this client (`None` if it is)."""
        self.stream_status_label.setText(message or "")
        self.stream_status_label.setVisible(message is not None)
        self.streamTakeOverButton.setVisible(message is not None)
        self.fftScopeWidget.set_stream_active(message is None)

    def setCentralWidget(self, widget):
        # Subclasses only set the central widget after our constructor.
        super().setCentralWidget(widget)
        widget.setEnabled(self._settings_enabled)

    def set_settings_enabled(self, enabled: bool):
        """Disable hardware controls and plots while their state is unconfirmed."""
        self._settings_enabled = enabled
        if self.centralWidget() is not None:
            self.centralWidget().setEnabled(enabled)
        self.menuBar().setEnabled(enabled)
        self.streamTakeOverButton.setEnabled(enabled)
        for child in self.findChildren(QDialog):
            child.setEnabled(enabled)

    def is_dark_theme(self):
        """Guess whether the current theme is dark or light by comparing the default text and
        background colors.
        """
        text_hsv_value = self.palette().color(QPalette.ColorRole.WindowText).value()
        bg_hsv_value = self.palette().color(QPalette.ColorRole.Window).value()
        return text_hsv_value > bg_hsv_value

    def _set_hardware_live_styling(self, is_live: bool):
        if is_live:
            self.stylesheet.pop("background-color", None)
            self.setWindowTitle(self._windowTitle)
        else:
            bg = "maroon" if self.is_dark_theme() else "mistyrose"
            self.stylesheet["background-color"] = bg
            self._windowTitle = self.windowTitle()
            self.setWindowTitle(f"{self._windowTitle} [OFFLINE]")

        self._setStyleSheet()

    def set_mqtt_configs(self, _stream_target: NetworkAddress):
        raise NotImplementedError

    def update_transfer_function(self, setting):
        """Update transfer function plot based on setting change."""
        if setting.app_root().name != "ui":
            return
        ui_iir = setting.get_parent_until(lambda x: x.name.startswith("iir"))
        if ui_iir is None:
            return

        ch = int(ui_iir.get_parent().name[2:])
        iir = int(ui_iir.name[3:])

        ba = settings_coefficients(self.fftScopeWidget.sample_period, ui_iir.value)

        try:
            self.channels[ch].iir_widgets[iir].update_transfer_function(ba)
        except NameError:
            logger.error("Unable to update transfer function: widget not found")
        except KeyError:
            logger.error(
                "Unable to update transfer function: incorrect number of channels")
