import os
import logging

from PyQt5 import QtGui, QtWidgets, uic
from stabilizer.stream import Parser, AdcDecoder, DacDecoder
from stabilizer import DEFAULT_L674_SAMPLE_PERIOD

from .lock import LockState
from ...utils import link_slider_to_spinbox
from ...stream.fft_scope import FftScope
from ...ui import AbstractUiWindow

logger = logging.getLogger(__name__)


class TextEditLogHandler(logging.Handler):

    def __init__(self, text_edit: QtWidgets.QTextEdit):
        super().__init__(level=logging.INFO)
        self.text_edit = text_edit
        self._text = ""
        self.formatter = logging.Formatter("<em>%(asctime)s</em>&nbsp; %(message)s",
                                           "%Y-%m-%d %H:%M:%S")

    def emit(self, record: logging.LogRecord):
        self._text += self.format(record) + "<br/>"
        self.text_edit.setText(self._text)
        self.text_edit.moveCursor(QtGui.QTextCursor.End)


class UiWindow(AbstractUiWindow):
    afe_options = ["G1", "G2", "G5", "G10"]

    def __init__(self, title: str = "Laser lock"):
        super().__init__()

        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "app.ui")
        uic.loadUi(ui_path, self)
        self.setWindowTitle(title)

        self._link_paired_widgets()

        self.comm_status_label = QtWidgets.QLabel()
        self.statusbar.addPermanentWidget(self.comm_status_label)

        # Explicitly create button group to prevent shortcuts from un-selecting buttons.
        # (It's not clear to me whether this is a Qt bug or not, as the buttons are set
        # as autoExclusive (the default) in the UI file.)
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_group.addButton(self.disablePztButton)
        self.mode_group.addButton(self.rampPztButton)
        self.mode_group.addButton(self.enablePztButton)

        self.log_handler = TextEditLogHandler(self.logOutputText)
        logger.addHandler(self.log_handler)

        self.lock_state = LockState.uninitialised

        for afe in [self.afe0GainBox, self.afe1GainBox]:
            afe.addItems(self.afe_options)

        streamParser = Parser([AdcDecoder(), DacDecoder()])
        self.scope = FftScope(streamParser, DEFAULT_L674_SAMPLE_PERIOD)
        self.tabWidget.addTab(self.scope, "Scope")

    def _link_paired_widgets(self):
        for s, b in [(self.fastPGainSlider, self.fastPGainBox),
                     (self.fastIGainSlider, self.fastIGainBox),
                     (self.notchFreqSlider, self.notchFreqBox),
                     (self.notchQSlider, self.notchQBox),
                     (self.slowPGainSlider, self.slowPGainBox),
                     (self.slowIGainSlider, self.slowIGainBox)]:
            link_slider_to_spinbox(s, b)

    def update_lock_state(self, state: LockState, adc1_voltage: float):
        self.lock_state = state
        color = {
            LockState.out_of_lock: "red",
            LockState.relocking: "yellow",
            LockState.locked: "green",
            LockState.uninitialised: "grey"
        }[state]
        self.adc1ReadingEdit.setStyleSheet(f"QLineEdit {{ background-color: {color} }}")
        if state == LockState.uninitialised:
            self.adc1ReadingEdit.setText("<pending>")
        else:
            self.adc1ReadingEdit.setText(f"{adc1_voltage * 1e3:0.0f} mV")

    def update_stream(self, payload):
        if self.tabWidget.currentIndex() != 1:
            return
        self.scope.update(payload)