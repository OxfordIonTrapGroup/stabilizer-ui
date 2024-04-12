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

    def _offlineMessageBox(self, hasPanicked: bool):
        messageBox = QMessageBox()

        if hasPanicked:
            messageBox.setText("Stabilizer panicked!")
            messageBox.setInformativeText("Please restart the stabilizer. You may need to change some settings before restart to prevent the issue from reoccuring.")
            messageBox.setIcon(QMessageBox.Critical)
        else:
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
        panicMessageBox.setInformativeText(f"Stabilizer might be panicked. "\
            "Please check and restart if necessary. " \
            "You may need to change some settings to prevent the issue from reoccuring.")
        panicMessageBox.setDescriptiveText(f"Diagnostic information: \n{value}")

        panicMessageBox.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        panicMessageBox.setDefaultButton(QMessageBox.Ok)
        panicMessageBox.setEscapeButton(QMessageBox.Cancel)

        overrideButton = panicMessageBox.button(QMessageBox.Cancel)
        overrideButton.setText("Override")
        overrideButton.setToolTip("The stabilizer is not actually panicked, override")

        acceptButton = panicMessageBox.button(QMessageBox.Ok)
        acceptButton.setText("Accept Panic")
        acceptButton.setToolTip("The stabilizer is indeed panicked")

        userInput = panicMessageBox.exec()

        match userInput:
            case QMessageBox.Ok:
                self.stylesheet["background-color"] = "mistyrose"
                self.setWindowTitle(f"{self._windowTitle} [PANICKED]")
            case QMessageBox.Cancel:
                self.stylesheet.pop("background-color", None)
                self.setWindowTitle(self._windowTitle)
        self._setStyleSheet()

    def onlineStatusChange(self, isOnline: bool, hasPanicked: bool):
        if hasPanicked:
            self.onPanicStatusChange(hasPanicked)
            return

        if self.stabilizerOnline == isOnline:
            return

        self.stabilizerOnline = isOnline

        if self.stabilizerOnline:
            self.stylesheet.pop("background-color")
            self.setWindowTitle(self._windowTitle)
        else:
            self.stylesheet["background-color"] = "mistyrose"
            self._offlineMessageBox(hasPanicked).exec()

            self._windowTitle = self.windowTitle()
            self.setWindowTitle(f"{self._windowTitle} [OFFLINE]")

        self._setStyleSheet()
