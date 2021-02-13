import argparse
import asyncio
import logging
import os
from PyQt5 import QtCore, QtWidgets, uic
from qasync import QEventLoop
import sys

from .ui_utils import link_slider_to_spinbox

logger = logging.getLogger(__name__)


class UI(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "app.ui")
        uic.loadUi(ui_path, self)

        self._link_paired_widgets()

        # Always boot into locks disabled. For simplicity, we never read back hardware
        # state. This could be added in the future.
        self._saved_widgets = {
            "fast_pid/p_gain": self.fastPGainBox,
            "fast_pid/i_gain": self.fastIGainBox,
            "fast_notch/enabled": self.notchGroup,
            "fast_notch/freq": self.notchFreqBox,
            "fast_notch/q": self.notchQBox,
            "slow_pid/enabled": self.slowPIDGroup,
            "slow_pid/p_gain": self.slowPGainBox,
            "slow_pid/i_gain": self.slowIGainBox,
            "adc1/ignore": self.adc1IgnoreButton,
            "adc1/fast_input": self.adc1FastInputButton,
            "adc1/fast_output": self.adc1FastOutputButton,
        }

    def _link_paired_widgets(self):
        for s, b in [(self.fastPGainSlider, self.fastPGainBox),
                     (self.fastIGainSlider, self.fastIGainBox),
                     (self.notchFreqSlider, self.notchFreqBox),
                     (self.notchQSlider, self.notchQBox),
                     (self.slowPGainSlider, self.slowPGainBox),
                     (self.slowIGainSlider, self.slowIGainBox)]:
            link_slider_to_spinbox(s, b)

    def load_state(self):
        settings = QtCore.QSettings()
        for path, widget in self._saved_widgets.items():
            val = settings.value(path)
            if val is None:
                continue
            if isinstance(
                    widget,
                (QtWidgets.QCheckBox, QtWidgets.QRadioButton, QtWidgets.QGroupBox)):
                widget.setChecked(val == "true")
            elif isinstance(widget, QtWidgets.QDoubleSpinBox):
                widget.setValue(float(val))
        geom = settings.value("window_geometry")
        if geom is not None:
            self.restoreGeometry(geom)

    def save_state(self):
        settings = QtCore.QSettings()
        settings.setValue("window_geometry", self.saveGeometry())
        for path, widget in self._saved_widgets.items():
            val = None
            if isinstance(
                    widget,
                (QtWidgets.QCheckBox, QtWidgets.QRadioButton, QtWidgets.QGroupBox)):
                val = widget.isChecked()
            elif isinstance(widget, QtWidgets.QDoubleSpinBox):
                val = widget.value()
            assert val is not None
            settings.setValue(path, val)

    def closeEvent(self, event):
        self.save_state()
        super().closeEvent(event)


async def stabilizer_task(ui: UI, stabilizer_host, stabilizer_port=1235):
    try:
        reader, writer = await asyncio.open_connection(stabilizer_host, stabilizer_port)

        def update_fast_iir():
            pass

        def update_notch():
            pass

        def update_slow_iir():
            pass

        def update_adc1_mode():
            pass

        update_fast_iir()
        update_notch()
        update_slow_iir()
        update_adc1_mode()

        ui.pztLockGroup.setEnabled(True)

    except Exception as e:
        if isinstance(e, asyncio.CancelledError):
            return
        logger.exception("Failure in Stabilizer communication task")
        ui.pztLockGroup.setEnabled(False)
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Interface for the Vescent + Stabilizer 674 laser lock setup")
    parser.add_argument("-s", "--stabilizer-host", default="10.34.16.103")
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName("674 lock UI")
    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)

        ui = UI()
        ui.load_state()
        ui.show()

        asyncio.create_task(stabilizer_task(ui, args.stabilizer_host))
        # TODO: Handle cancellation.

        sys.exit(loop.run_forever())


if __name__ == "__main__":
    main()
