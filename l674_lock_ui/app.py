import argparse
import asyncio
from contextlib import suppress
import json
import logging
import os
from PyQt5 import QtCore, QtWidgets, uic
from qasync import QEventLoop
import sys

from .iir import *
from .ui_utils import link_slider_to_spinbox

logger = logging.getLogger(__name__)

AOM_LOCK_GPIO_IDX = 1


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

        self.comm_status_label = QtWidgets.QLabel()
        self.statusbar.addPermanentWidget(self.comm_status_label)

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


class StabilizerError(Exception):
    pass


async def update_stabilizer(ui: UI, host: str, port: int = 1235):
    try:
        reader, writer = await asyncio.open_connection(host, port)

        async def query(msg):
            s = json.dumps(msg, separators=[",", ":"])
            assert "\n" not in s
            writer.write((s + "\n").encode("ascii"))
            logger.info("[stabilizer] Sent: %s", s)

            r = (await asyncio.wait_for(reader.readline(), timeout=1.0)).decode()
            logger.info("[stabilizer] Recv: %s", r)

            ret = json.loads(r)
            if ret["code"] != 200:
                raise StabilizerError(ret)
            return ret

        async def write(attr, value):
            val_json = json.dumps(value, separators=[",", ":"]).replace('"', "'")
            await query({
                "req": "Write",
                "attribute": attr,
                "value": val_json,
            })

        async def update_biquad(name, channel, coeffs):
            await write(f"stabilizer/iir{name}/state", {
                "channel": channel,
                "iir": iir_config(coeffs)
            })

        async def update_fast_iir():
            if ui.disablePztButton.isChecked():
                kp = ki = 0
            elif ui.rampPztButton.isChecked():
                kp = 5
                ki = 0
            elif ui.enablePztButton.isChecked():
                kp = -ui.fastPGainBox.value()
                ki = ui.fastIGainBox.value() * 1e3
            else:
                assert False
            await update_biquad("0", 0, pi_coeffs(kp=kp, ki=ki))

        async def update_notch():
            if ui.notchGroup.isChecked():
                coeffs = notch_coeffs(ui.notchFreqBox.value() * 1e3,
                                      ui.notchQBox.value())
            else:
                # Pass through.
                coeffs = pi_coeffs(kp=1.0, ki=0.0)
            await update_biquad("_b0", 0, coeffs)

        async def update_slow_iir():
            if ui.enablePztButton.isChecked() and ui.slowPIDGroup.isChecked():
                kp = ui.slowPGainBox.value()
                ki = ui.slowIGainBox.value()
            else:
                kp = ki = 0
            await update_biquad("1", 1, pi_coeffs(kp=kp, ki=ki))

        async def update_adc1_mode():
            if ui.adc1IgnoreButton.isChecked():
                value = "ignore"
            elif ui.adc1FastInputButton.isChecked():
                value = "sum_with_adc0"
            elif ui.adc1FastOutputButton.isChecked():
                value = "sum_with_iir0_output"
            else:
                assert False
            await write(f"stabilizer/route_adc1", value)

        # Query in most glitchless order.
        await update_adc1_mode()
        await update_notch()
        await update_fast_iir()
        await update_slow_iir()

        fast_iir_changed = asyncio.Event()
        notch_changed = asyncio.Event()
        slow_iir_changed = asyncio.Event()
        adc1_changed = asyncio.Event()

        pid_state_widgets = [ui.disablePztButton, ui.rampPztButton, ui.enablePztButton]
        links = {
            fast_iir_changed: [
                ui.fastPGainBox,
                ui.fastIGainBox,
            ] + pid_state_widgets,
            notch_changed: [
                ui.notchGroup,
                ui.notchFreqBox,
                ui.notchQBox,
            ],
            slow_iir_changed: [
                ui.slowPGainBox,
                ui.slowIGainBox,
                ui.slowPIDGroup,
            ] + pid_state_widgets,
            adc1_changed: [
                ui.adc1IgnoreButton,
                ui.adc1FastInputButton,
                ui.adc1FastOutputButton,
            ]
        }
        for event, widgets in links.items():
            # Capture loop variable.
            set_ev = (lambda ev: lambda *args: ev.set())(event)
            for widget in widgets:
                if hasattr(widget, "valueChanged"):
                    widget.valueChanged.connect(set_ev)
                elif hasattr(widget, "toggled"):
                    widget.toggled.connect(set_ev)
                else:
                    assert False

        ui.pztLockGroup.setEnabled(True)

        while True:
            done_tasks, _ = await asyncio.wait([event.wait() for event in links.keys()],
                                               timeout=1.0,
                                               return_when=asyncio.FIRST_COMPLETED)
            if adc1_changed.is_set():
                await update_adc1_mode()
                adc1_changed.clear()
            if notch_changed.is_set():
                await update_notch()
                notch_changed.clear()
            if fast_iir_changed.is_set():
                await update_fast_iir()
                fast_iir_changed.clear()
            if slow_iir_changed.is_set():
                await update_slow_iir()
                slow_iir_changed.clear()
            if not done_tasks:
                # TODO: Ping.
                pass

    except Exception as e:
        if isinstance(e, asyncio.CancelledError):
            return
        logger.exception("Failure in Stabilizer communication task")
        ui.pztLockGroup.setEnabled(False)

        text = str(e)
        if not text:
            # Show message for things like timeout errors.
            text = repr(e)
        ui.comm_status_label.setText(f"Stabilizer connection error: {text}")
        raise


def main():
    logging.basicConfig(level=logging.INFO)

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

        ui.comm_status_label.setText(
            f"Connecting to Stabilizer at {args.stabilizer_host}…")
        stabilizer_task = asyncio.create_task(
            update_stabilizer(ui, args.stabilizer_host))

        gpio_dongle = None
        try:
            from .cp2102n_usb_to_uart_driver import CP2102N
            gpio_dongle = CP2102N()

            def update_gpio():
                gpio_dongle.set_gpio(AOM_LOCK_GPIO_IDX,
                                     1 if ui.enableAOMLockBox.isChecked() else 0)

            ui.enableAOMLockBox.toggled.connect(update_gpio)
            update_gpio()
        except Exception as e:
            ui.comm_status_label.setText(f"GPIO dongle intialisation failed: {e}")

        try:
            sys.exit(loop.run_forever())
        finally:
            if gpio_dongle is not None:
                gpio_dongle.reset()
            with suppress(asyncio.CancelledError):
                stabilizer_task.cancel()
                loop.run_until_complete(stabilizer_task)


if __name__ == "__main__":
    main()
