from PyQt5.QtWidgets import QMainWindow, QMessageBox
from typing import Optional

class AbstractUiWindow(QMainWindow):
    """Abstract class for main UI window"""

    def __init__(self):
        super().__init__()

        self.stabilizerOnline = True
        self.stylesheet = {}

    def _setStyleSheet(self):
        stylesheet_str = ";".join([f"{key}: {value}" for key, value in self.stylesheet.items()])
        self.setStyleSheet(stylesheet_str)

    def _offlineMessageBox(self):
        messageBox = QMessageBox()

        messageBox.setText("Stabilizer offline")
        messageBox.setInformativeText("Check the stabilizer's network connection.")
        messageBox.setIcon(QMessageBox.Warning)

        messageBox.setStandardButtons(QMessageBox.Ok)

        return messageBox

    def onPanicStatusChange(self, isPanicked: bool, value: Optional[str]):
        if not isPanicked:
            self.stylesheet.pop("background-color")
            self.setWindowTitle(self._windowTitle)
            return
        
        panicMessageBox = QMessageBox()
        panicMessageBox.setText("Stabilizer panicked!")
        panicMessageBox.setIcon(QMessageBox.Critical)
        panicMessageBox.setInformativeText(f"Stabilizer had panicked, but has since restarted. "\
            "You may need to change some settings if the issue persists.")
        panicMessageBox.setDescriptiveText(f"Diagnostic information: \n{value}")

        panicMessageBox.setStandardButtons(QMessageBox.Ok)
        panicMessageBox.setDefaultButton(QMessageBox.Ok)
        panicMessageBox.setEscapeButton(QMessageBox.Ok)

        panicMessageBox.exec()

    def onlineStatusChange(self, isOnline: bool):
        if self.stabilizerOnline == isOnline:
            return

        self.stabilizerOnline = isOnline

        if self.stabilizerOnline:
            self.stylesheet.pop("background-color")
            self.setWindowTitle(self._windowTitle)
        else:
            self.stylesheet["background-color"] = "mistyrose"
            self._offlineMessageBox().exec()

            self._windowTitle = self.windowTitle()
            self.setWindowTitle(f"{self._windowTitle} [OFFLINE]")

        self._setStyleSheet()
