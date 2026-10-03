from __future__ import annotations

import logging
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QMainWindow, QDialog, QInputDialog, QMenu, QMessageBox,
                             QLabel, QPushButton)
from PyQt6.QtGui import QPalette
from typing import Optional, TYPE_CHECKING

from .iir.filters import settings_coefficients

if TYPE_CHECKING:
    from .firmware import Firmware
    from .stream.thread import StreamThread

logger = logging.getLogger(__name__)


class AbstractUiWindow(QMainWindow):
    """Abstract class for main UI window

    Subclasses are expected to have the widgets for the device settings in
    `channelTabWidget`, and the scope as `fftScopeWidget`, and to set the window title
    to the name of the application (the device is added by `set_device()`).
    """

    #: Emitted with the new name when the user renames the device.
    deviceRenamed = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        #: The MQTT ID of the device and its name (empty if it has none), see
        #: `set_device()`.
        self.device_id = ""
        self.device_name = ""
        #: The window title without the device, once known.
        self._title: Optional[str] = None
        self._connection_is_nominal = True
        self.stylesheet = {}
        self._tools_menu = None
        #: The map from topic to widgets, set by `set_mqtt_configs()`.
        self._settings_map = {}
        self._stream_thread = None
        self._stream_device = None
        #: Whether the device settings are confirmed, see `set_settings_enabled()`.
        self._settings_enabled = False
        #: The firmware of the device, once known (see `set_firmware()`).
        self.firmware: Optional[Firmware] = None

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

        self.renameAction = self.menuBar().addMenu("&Device").addAction("&Rename…")
        self.renameAction.triggered.connect(self._rename_clicked)

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

    @property
    def device_label(self) -> str:
        """The name of the device, or its ID if it has none."""
        return self.device_name or self.device_id

    def set_device(self, device_id: str, name: str = ""):
        """Show the device (by `name`, or the ID if it is empty) in the window title."""
        self.device_id = device_id
        self.set_device_name(name)

    def set_device_name(self, name: str):
        """Show the device by `name` (by its ID if it is empty), in the window title and
        the names of the files written."""
        self.device_name = name
        self._update_title()
        self._stream_device = self.device_label
        self.fftScopeWidget.record_bar.set_device(self.device_label)

    def rename_device(self, name: str):
        """Give the device a new name, as the user does."""
        self.set_device_name(name.strip())
        self.deviceRenamed.emit(self.device_name)

    def _rename_clicked(self):
        name, ok = QInputDialog.getText(
            self,
            "Rename device",
            f"Name of {self.device_id}, shown in the window title and the list of "
            "devices, for everybody\n(stored on the MQTT broker; leave it empty for "
            "none):",
            text=self.device_name)
        if ok:
            self.rename_device(name)

    def _update_title(self):
        if self._title is None:
            # As set by the subclass.
            self._title = self.windowTitle()
        title = self._title
        if self.device_label:
            title += f" [{self.device_label}]"
        if not self._connection_is_nominal:
            title += " [OFFLINE]"
        self.setWindowTitle(title)

    def tools_menu(self) -> QMenu:
        """The Tools menu (added on first use)."""
        if self._tools_menu is None:
            self._tools_menu = self.menuBar().addMenu("&Tools")
        return self._tools_menu

    def set_stream_thread(self, stream_thread: StreamThread, device: str):
        """Enable the tools using the stream data, fed by the given `StreamThread`."""
        self._stream_thread = stream_thread
        self._stream_device = device
        self.fftScopeWidget.record_bar.set_stream_thread(stream_thread, device,
                                                         self.settings_snapshot)

    def settings_snapshot(self) -> dict:
        """The current settings (by topic) the device has, and the version of its
        firmware (`firmware`), to store with recorded data."""
        firmware = self.firmware
        snapshot = {
            key: cfg.read_handler(cfg.widgets)
            for key, cfg in self._settings_map.items()
            if firmware is None or firmware.has(key)
        }
        if firmware is not None:
            snapshot["firmware"] = firmware.name
        return snapshot

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
        else:
            bg = "maroon" if self.is_dark_theme() else "mistyrose"
            self.stylesheet["background-color"] = bg
        self._update_title()
        self._setStyleSheet()

    def set_mqtt_configs(self, _stream_target: NetworkAddress):
        raise NotImplementedError

    def set_firmware(self, firmware: Firmware):
        """Called with the firmware of the device each time it has connected, to disable
        what it does not support (beyond the widgets bound to settings it does not
        have, which are disabled already)."""
        self.firmware = firmware

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
