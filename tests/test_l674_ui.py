"""The `l674` window and interface: the lock mode override of the biquads, the UI state of
the earlier layout, the bindings, and the designed controller response."""
import asyncio
import json
import os
import sys

import numpy as np
import pytest
from scipy import signal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets  # noqa: E402

from stabilizer_ui.mqtt import NetworkAddress, UiMqttBridge, read, write  # noqa: E402
from stabilizer_ui.target.l674 import interface as l674_interface  # noqa: E402
from stabilizer_ui.target.l674.lock import LockState  # noqa: E402
from stabilizer_ui.target.l674.ui import UiWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


@pytest.fixture
def window(app):
    window = UiWindow()
    window.set_mqtt_configs(NetworkAddress([127, 0, 0, 1], 9293))
    yield window
    window.logView.close_handlers()
    window.close()


def sync_topics(window, interface):
    for key, cfg in window._settings_map.items():
        interface.app_root.get_or_create_child(key).value = cfg.read_handler(cfg.widgets)


def test_lock_mode_override(window):
    """The lock mode decides the first biquad of each channel; the others are as
    designed."""
    interface = l674_interface.StabilizerInterface()
    sync_topics(window, interface)
    fast = window.channels[0].iir_widgets[0]
    fast.filterComboBox.setCurrentText("pid")
    fast.widgets["pid"].KpBox.setValue(-0.1)
    fast.y_maxBox.setValue(5.0)
    notch = window.channels[0].iir_widgets[1]
    notch.filterComboBox.setCurrentText("notch")
    sync_topics(window, interface)
    nodes = {
        name: interface.app_root.child(f"ui/{name}")
        for name in ["ch0/iir0", "ch0/iir1", "ch1/iir0", "ch1/iir1"]
    }

    def ba(name):
        return interface._biquad_value(nodes[name])["coeff"]["ba"]

    for mode, fast_ba, slow_ba in [
        ("Disabled", [0.0] * 5, [0.0] * 5),
        ("RampPassThrough", [l674_interface.RAMP_PASS_THROUGH_GAIN, 0.0, 0.0, 0.0,
                             0.0], [0.0] * 5),
    ]:
        interface.app_root.child(l674_interface.LOCK_MODE_KEY).value = mode
        assert interface.lock_mode() == mode
        assert ba("ch0/iir0") == fast_ba
        assert ba("ch1/iir0") == slow_ba
        value = interface._biquad_value(nodes["ch0/iir0"])
        assert value["u"] == 0
        assert value["max"] == 5.0 * 32768 / 10.24
        assert ba("ch0/iir1")[0] != 1.0 and ba("ch0/iir1")[3] != 0.0, "notch as designed"
        assert ba("ch1/iir1") == [1, 0, 0, 0, 0]

    interface.app_root.child(l674_interface.LOCK_MODE_KEY).value = "Enabled"
    assert ba("ch0/iir0") == [-0.1, 0, 0, 0, 0]
    assert ba("ch1/iir0") == [1, 0, 0, 0, 0]

    interface.app_root.child(l674_interface.LOCK_MODE_KEY).value = None
    assert interface.lock_mode() == "Disabled"


def test_lock_mode_binding(window):
    """The radio buttons give the mode, and showing a mode checks the button."""
    cfg = window._settings_map[l674_interface.LOCK_MODE_KEY]
    assert cfg.read_handler(cfg.widgets) == "Disabled"
    cfg.write_handler(cfg.widgets, "Enabled")
    assert window.enablePztButton.isChecked() and not window.disablePztButton.isChecked()
    assert cfg.read_handler(cfg.widgets) == "Enabled"
    window.rampPztButton.setChecked(True)
    assert cfg.read_handler(cfg.widgets) == "RampPassThrough"


def test_bindings(window):
    """The device settings and the relocking parameters are bound as expected."""
    m = window._settings_map
    assert set(key for key in m if not key.startswith("ui/ch")) == {
        "settings/stream", "settings/ch/0/gain", "settings/ch/0/run",
        "settings/ch/1/gain", "settings/ch/1/run", "settings/gain_ramp_time",
        "settings/lock_detect/threshold", "settings/lock_detect/reset_time",
        "settings/aux_ttl_out", "ui/lock_mode", "ui/relock"
    }
    # The AOM lock box is the inverted auxiliary output.
    aux = m["settings/aux_ttl_out"]
    window.enableAOMLockBox.setChecked(True)
    assert aux.read_handler(aux.widgets) is False
    aux.write_handler(aux.widgets, True)
    assert not window.enableAOMLockBox.isChecked()
    # Channel 1's AFE gain is the ADC1 gain box with the lock detection.
    assert window.channels[1].afeGainBox is window.afe1GainBox
    assert m["settings/ch/1/gain"].widgets == [window.afe1GainBox]
    assert window.afe1GainBox.count() == 4
    # The relocking parameters (line edits).
    relock = m["ui/relock"]
    relock.write_handler(
        relock.widgets, {
            "wand_server": "10.0.0.1:3251",
            "wand_channel": "lab1_674",
            "solstis_host": "10.0.0.2"
        })
    assert window.wandServerEdit.text() == "10.0.0.1:3251"
    assert relock.read_handler(relock.widgets) == {
        "wand_server": "10.0.0.1:3251",
        "wand_channel": "lab1_674",
        "solstis_host": "10.0.0.2"
    }
    assert window.relock_config()["solstis_host"] == "10.0.0.2"
    # Defaults only fill in empty parameters.
    window.set_relock_defaults("1.1.1.1:1", None, "2.2.2.2")
    assert window.wandServerEdit.text() == "10.0.0.1:3251"
    window.wandServerEdit.setText("")
    window.set_relock_defaults("1.1.1.1:1", None, "2.2.2.2")
    assert window.wandServerEdit.text() == "1.1.1.1:1"


def test_line_edit_binding(app):
    edit = QtWidgets.QLineEdit()
    write([edit], "abc")
    assert read([edit]) == "abc"
    bridge = UiMqttBridge.__new__(UiMqttBridge)
    # Only what `connect_ui()` needs.
    bridge.configs = {"ui/x": type("Cfg", (), {"widgets": [edit]})()}
    bridge._showing = False
    bridge.keys_to_write = set()
    bridge.updated = asyncio.Event()
    bridge.connect_ui()
    edit.setText("typed")
    assert not bridge.keys_to_write, "no write while typing"
    edit.editingFinished.emit()
    assert bridge.keys_to_write == {"ui/x"}


def test_legacy_ui_state(window):
    """The gains of the earlier `l674` UI are shown as the current filter settings."""
    legacy_map = window.legacy_ui_map()
    client = type("Client", (), {"subscribe": lambda *a, **k: None})()
    bridge = UiMqttBridge(client, window._settings_map)
    bridge.legacy_map = legacy_map
    shown = []
    bridge.on_ui_value = shown.append
    bridge._loading = True
    legacy = {
        "ui/fast_gains/proportional": 0.05,
        "ui/fast_gains/integral": 2500.0,
        "ui/fast_notch_enable": True,
        "ui/fast_notch/frequency": 12000.0,
        "ui/fast_notch/quality_factor": 3.0,
        "ui/slow_gains/proportional": 1e-4,
        "ui/slow_gains/integral": 0.25,
        "ui/slow_enable": True,
        "ui/lock_mode": "Enabled",
    }
    for topic, value in legacy.items():
        bridge._handle_ui_message(topic,
                                  json.dumps(value).encode(), {"retain": True}, True)
    bridge._loading = False
    bridge._show_legacy_values()

    fast = window.channels[0].iir_widgets[0]
    assert fast.filterComboBox.currentText() == "pid"
    assert fast.widgets["pid"].KpBox.value() == -0.05
    assert fast.widgets["pid"].KiBox.value() == pytest.approx(-2.5)  # kHz
    notch = window.channels[0].iir_widgets[1]
    assert notch.filterComboBox.currentText() == "notch"
    assert notch.widgets["notch"].f0Box.value() == pytest.approx(12.0)  # kHz
    assert notch.widgets["notch"].QBox.value() == 3.0
    assert notch.widgets["notch"].KBox.value() == 1.0
    slow = window.channels[1].iir_widgets[0]
    assert slow.filterComboBox.currentText() == "pid"
    assert slow.widgets["pid"].KpBox.value() == 1e-4
    assert slow.widgets["pid"].KiBox.value() == pytest.approx(0.25e-3)
    assert window.enablePztButton.isChecked()
    assert set(shown) == {"ui/ch0/iir0", "ui/ch0/iir1", "ui/ch1/iir0", "ui/lock_mode"}
    assert not bridge.keys_to_write, "nothing queued for writing"

    # A value retained in the current layout takes precedence.
    bridge._loading = True
    bridge._handle_ui_message("ui/slow_enable", b"false", {"retain": True}, True)
    bridge._handle_ui_message("ui/ch1/iir0",
                              json.dumps({
                                  "filter": "through"
                              }).encode(), {"retain": True}, True)
    bridge._loading = False
    bridge._show_legacy_values()
    assert slow.filterComboBox.currentText() == "through"


def test_controller_sos(window):
    """The designed controller response is the cascade of the available biquads of the
    channel, including the AFE gain."""
    channel = window.channels[0]
    pi = [0.5, -0.4, 0.0, 1.0, 0.0]
    notch = [0.9, -1.7, 0.9, 1.7, -0.8]
    channel.iir_widgets[0].update_transfer_function(pi)
    channel.iir_widgets[1].update_transfer_function(notch)
    channel.afeGainBox.setCurrentText("G2")
    channel.runModeBox.setCurrentText("Run")

    w = np.linspace(0.01, np.pi, 64)  # (not at the pole of the integrator)

    def response(ba):
        return signal.freqz(ba[:3], [1, -ba[3], -ba[4]], worN=w)[1]

    sos = window.settings_snapshot()["controller_sos"]["0"]
    np.testing.assert_allclose(
        signal.sosfreqz(sos, worN=w)[1], 2 * response(pi) * response(notch))

    channel.set_iir_available(1, False)
    np.testing.assert_allclose(
        signal.sosfreqz(channel.controller_sos(), worN=w)[1], 2 * response(pi))

    channel.runModeBox.setCurrentText("Hold")
    assert channel.controller_sos() is None


def test_lock_state_display(window):
    window.update_lock_state(LockState.locked, 0.4321)
    assert window.adc1ReadingEdit.text() == "432 mV"
    assert "green" in window.adc1ReadingEdit.styleSheet()
    window.update_lock_state(LockState.uninitialised, None)
    assert window.adc1ReadingEdit.text() == "<pending>"
    assert window.lock_state == LockState.uninitialised


def test_settings_widgets(window):
    """The log stays enabled while the settings are not."""
    window.set_settings_enabled(False)
    assert not window.settings_panel.isEnabled()
    assert not window.fftScopeWidget.isEnabled()
    assert window.logView.isEnabled()
    window.set_settings_enabled(True)
    assert window.settings_panel.isEnabled()
