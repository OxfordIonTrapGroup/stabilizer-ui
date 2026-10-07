import asyncio
import logging
import os
from typing import Optional

from PyQt6 import QtCore, QtWidgets, uic
from stabilizer.stream_parser import Parser, AdcDecoder

from . import *
from .interface import LOCK_MODES, StabilizerInterface
from .lock import LockMonitor, LockState
from .log import LogView
from .solstis import parse_host
from .topics import StabilizerSettings, UiSettings
from .wavemeter import WavemeterInterface

from ...ui import AbstractUiWindow
from ...mqtt import NetworkAddress, UiMqttConfig, combine_configs
from ...iir.channel_settings import AbstractChannelSettings
from ...stream.fft_scope import FftScope
from ...spectral_density import SpectralDensityMixin
from ...stream.decoders import DacDecoder
from ...transfer_function.dialog import TransferFunctionMixin
from ...utils import invert, radio_group

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_SIZE = (1500, 800)
DEFAULT_DAC_PLOT_YRANGE = (-1, 1)
DEFAULT_ADC_PLOT_YRANGE = (-1, 1)

#: Colour of the transmission reading for each lock state.
STATE_COLOURS = {
    LockState.out_of_lock: "red",
    LockState.relocking: "yellow",
    LockState.locked: "green",
    LockState.uninitialised: "grey",
}

#: Time after the last edit of the relocking parameters until the monitor is restarted
#: with them, in milliseconds.
RELOCK_CONFIG_DELAY = 1000

#: Timeout for wavemeter requests. Should be at least a few seconds at least to
#: accommodate long exposure times on other channels, but short enough to recover from
#: lost connections while auto-relocking in a timely fashion.
WAVEMETER_TIMEOUT = 10.0


def _ui_path(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), "widgets", name)


class FastChannelSettings(AbstractChannelSettings):
    """The fast PZT channel (channel 0): the error signal on ADC0 through two biquads (a
    PI controller and a notch)."""

    def __init__(self, sample_period):
        super().__init__()
        uic.loadUi(_ui_path("fast_channel.ui"), self)
        self._add_afe_options()
        self._add_iir_tabWidget(sample_period)


class SlowChannelSettings(AbstractChannelSettings):
    """The slow PZT channel (channel 1): the output of channel 0 through two biquads.

    Its AFE gain is that of ADC1 (the cavity transmission), which is shown with the lock
    detection; `afe_gain_box` is that combo box.
    """

    def __init__(self, sample_period, afe_gain_box: QtWidgets.QComboBox):
        super().__init__()
        uic.loadUi(_ui_path("slow_channel.ui"), self)
        self.afeGainBox = afe_gain_box
        self._add_afe_options()
        self._add_iir_tabWidget(sample_period)


class _LockControl:
    """The `lock.LockControl` of the window: the relocking sets the widgets as a user
    would, and waits for the settings to reach the device."""

    def __init__(self, window: "UiWindow", interface: StabilizerInterface):
        self._window = window
        self._interface = interface

    def threshold(self) -> float:
        return self._window.lockDetectThresholdBox.value()

    async def read_transmission(self) -> float:
        return await self._interface.read_transmission()

    async def set_aom_lock(self, enabled: bool):
        self._window.enableAOMLockBox.setChecked(enabled)
        await self._interface.flush()

    async def set_pzt_lock(self, enabled: bool):
        window = self._window
        (window.enablePztButton if enabled else window.disablePztButton).setChecked(True)
        await self._interface.flush()

    def show_state(self, state: LockState, transmission: Optional[float]):
        self._window.update_lock_state(state, transmission)


class UiWindow(TransferFunctionMixin, SpectralDensityMixin, AbstractUiWindow):
    """The 674 nm laser lock: the lock controls and filters on the left, the scope and the
    log in tabs on the right."""

    #: Names of the widgets of the lock panel (`widgets/lock.ui`) available on the window.
    PANEL_WIDGETS = [
        "aomLockGroup", "enableAOMLockBox", "pztLockGroup", "disablePztButton",
        "rampPztButton", "enablePztButton", "gainRampTimeBox", "channelTabWidget",
        "adc1Group", "lockDetectThresholdBox", "lockDetectDelayBox", "afe1GainBox",
        "adc1ReadingEdit", "relockGroup", "enableRelockingBox", "relockStatusLabel",
        "wandServerEdit", "wandChannelEdit", "solstisHostEdit"
    ]

    def __init__(self, title: str = "674 lock"):
        super().__init__()
        self.setWindowTitle(title)

        self.lock_state = LockState.uninitialised
        self._interface: Optional[StabilizerInterface] = None
        self._monitor: Optional[LockMonitor] = None
        self._monitor_task: Optional[asyncio.Task] = None

        splitter = QtWidgets.QSplitter(self)
        self.setCentralWidget(splitter)

        # The lock controls and filters.
        self.settings_panel = QtWidgets.QWidget()
        uic.loadUi(_ui_path("lock.ui"), self.settings_panel)
        for name in self.PANEL_WIDGETS:
            setattr(self, name, getattr(self.settings_panel, name))
        splitter.addWidget(self.settings_panel)

        # Explicitly create button groups to prevent shortcuts from un-selecting buttons.
        # (It's not clear to me whether this is a Qt bug or not, as the buttons are set
        # as autoExclusive (the default) in the UI file.)
        self.mode_group = QtWidgets.QButtonGroup(self)
        for button in self.lock_mode_buttons():
            self.mode_group.addButton(button)

        self.channels = [
            FastChannelSettings(SAMPLE_PERIOD),
            SlowChannelSettings(SAMPLE_PERIOD, self.afe1GainBox),
        ]
        for channel, name in zip(self.channels,
                                 ["Fast PZT (channel 0)", "Slow PZT (channel 1)"]):
            self.channelTabWidget.addTab(channel, name)

        # The scope and the log.
        self.viewTabWidget = QtWidgets.QTabWidget()
        streamParser = Parser([AdcDecoder(), DacDecoder()])
        self.fftScopeWidget = FftScope(streamParser, SAMPLE_PERIOD)
        for i in range(NUM_CHANNELS):
            self.fftScopeWidget.graphics_view.getItem(
                0, i).setYRange(*DEFAULT_ADC_PLOT_YRANGE)
            self.fftScopeWidget.graphics_view.getItem(
                1, i).setYRange(*DEFAULT_DAC_PLOT_YRANGE)
        self.viewTabWidget.addTab(self.fftScopeWidget, "Scope")
        self.logView = LogView(__name__.rsplit(".", 1)[0])
        self.viewTabWidget.addTab(self.logView, "Log")
        splitter.addWidget(self.viewTabWidget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        # Disable mouse wheel scrolling on spinboxes to prevent accidental changes
        for box in self.settings_panel.findChildren(QtWidgets.QAbstractSpinBox):
            box.wheelEvent = lambda *event: None

        self.update_lock_state(LockState.uninitialised, None)

        self._relock_config_timer = QtCore.QTimer(self)
        self._relock_config_timer.setSingleShot(True)
        self._relock_config_timer.setInterval(RELOCK_CONFIG_DELAY)
        self._relock_config_timer.timeout.connect(self._restart_lock_monitor)
        for edit in self.relock_config_edits():
            edit.textChanged.connect(self._relock_config_changed)
        self.enableRelockingBox.toggled.connect(self._relock_enable_changed)

        self.resize(*DEFAULT_WINDOW_SIZE)
        splitter.setSizes([DEFAULT_WINDOW_SIZE[0] // 2, DEFAULT_WINDOW_SIZE[0] // 2])

        self._add_transfer_function_button()
        self._add_spectral_density_button()

    def lock_mode_buttons(self) -> list:
        """The lock mode radio buttons, in the order of `LOCK_MODES`."""
        return [self.disablePztButton, self.rampPztButton, self.enablePztButton]

    def relock_config_edits(self) -> list:
        return [self.wandServerEdit, self.wandChannelEdit, self.solstisHostEdit]

    def settings_widgets(self) -> list:
        # The log stays readable while the device is offline. (Called by the base class
        # before the widgets exist.)
        widgets = [
            getattr(self, "settings_panel", None),
            getattr(self, "fftScopeWidget", None)
        ]
        return [widget for widget in widgets if widget is not None]

    def update_stream(self, payload):
        if self.viewTabWidget.currentWidget() is self.fftScopeWidget:
            self.fftScopeWidget.update(payload)

    #
    # Lock state.
    #

    def update_lock_state(self, state: LockState, transmission: Optional[float]):
        """Show the lock state (and the transmission reading it is derived from)."""
        self.lock_state = state
        colour = STATE_COLOURS[state]
        self.adc1ReadingEdit.setStyleSheet(
            f"QLineEdit {{ background-color: {colour}; color: black; }}")
        if transmission is None:
            self.adc1ReadingEdit.setText("<pending>")
        else:
            self.adc1ReadingEdit.setText(f"{transmission * 1e3:0.0f} mV")
        self.adc1ReadingEdit.setToolTip(state.value)

    @property
    def transmission(self) -> Optional[float]:
        """The last transmission reading, in volts (`None` if there is none)."""
        return self._monitor.transmission if self._monitor is not None else None

    #
    # Relocking.
    #

    def set_relock_defaults(self, wand_server: Optional[str], wand_channel: Optional[str],
                            solstis_host: Optional[str]):
        """Fill in the relocking parameters which are empty (those retained on the broker
        replace them once loaded)."""
        for edit, value in zip(self.relock_config_edits(),
                               [wand_server, wand_channel, solstis_host]):
            if value and not edit.text():
                edit.setText(value)

    def relock_config(self) -> dict:
        return {
            "wand_server": self.wandServerEdit.text().strip(),
            "wand_channel": self.wandChannelEdit.text().strip(),
            "solstis_host": self.solstisHostEdit.text().strip(),
        }

    def start_lock_monitor(self, interface: StabilizerInterface):
        """Start monitoring the lock (and relocking) through `interface`."""
        self._interface = interface
        self._restart_lock_monitor()

    def stop_lock_monitor(self):
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None
        self._monitor = None

    def _relock_config_changed(self, _text):
        if self._interface is not None:
            self._relock_config_timer.start()

    def _restart_lock_monitor(self):
        if self._interface is None:
            return
        self.stop_lock_monitor()
        config = self.relock_config()
        wavemeter = solstis = None
        status = []
        try:
            if config["wand_server"] and config["wand_channel"]:
                host, port = parse_host(config["wand_server"], 3251)
                wavemeter = WavemeterInterface(host, port, config["wand_channel"],
                                               WAVEMETER_TIMEOUT)
            else:
                status.append("no wavemeter configured")
            if config["solstis_host"]:
                solstis = parse_host(config["solstis_host"])
            else:
                status.append("no SolsTiS configured")
        except ValueError as e:
            status.append(f"invalid relocking parameters ({e})")
            wavemeter = solstis = None
        self.relockStatusLabel.setText(", ".join(status).capitalize())
        self.enableRelockingBox.setEnabled(False)
        self._monitor = LockMonitor(_LockControl(self, self._interface), wavemeter,
                                    solstis, self.enableRelockingBox.isChecked,
                                    self._relock_available)
        self._monitor_task = asyncio.ensure_future(self._run_monitor(self._monitor))

    async def _run_monitor(self, monitor: LockMonitor):
        try:
            await monitor.run()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Lock monitor failed")
            self.relockStatusLabel.setText("Lock monitor failed, see the log")

    def _relock_available(self, available: bool):
        """Enable relocking if the wavemeter answers (as the UI starts), and disable it
        otherwise."""
        self.enableRelockingBox.setEnabled(available)
        self.enableRelockingBox.setChecked(available)
        if available:
            self.relockStatusLabel.setText("Wavemeter connected")
        elif not self.relockStatusLabel.text():
            self.relockStatusLabel.setText("Wavemeter not reachable")

    def _relock_enable_changed(self, enabled: bool):
        if self._monitor is not None:
            self._monitor.set_relock_enabled(enabled)

    #
    # MQTT.
    #

    def set_mqtt_configs(self, stream_target: NetworkAddress):
        """ Link the UI widgets to the MQTT topic tree"""

        # `ui/#` are only used by the UI, the others by both UI and stabilizer
        settings_map = {
            StabilizerSettings.stream.path():
            UiMqttConfig(
                [],
                lambda _: str(stream_target),
                lambda _w, _v: str(stream_target),
            )
        }

        for ch in range(NUM_CHANNELS):
            settings_map[StabilizerSettings.afes[ch].path()] = UiMqttConfig(
                [self.channels[ch].afeGainBox])
            settings_map[StabilizerSettings.runs[ch].path()] = UiMqttConfig(
                [self.channels[ch].runModeBox])

            # IIR settings
            for iir in range(NUM_IIR_FILTERS_PER_CHANNEL):
                iirWidget = self.channels[ch].iir_widgets[iir]
                iir_topic = UiSettings.iirs[ch][iir]

                iirWidget.set_mqtt_configs(settings_map, iir_topic)

        settings_map[StabilizerSettings.gain_ramp_time.path()] = UiMqttConfig(
            [self.gainRampTimeBox])
        settings_map[StabilizerSettings.ld_threshold.path()] = UiMqttConfig(
            [self.lockDetectThresholdBox])
        settings_map[StabilizerSettings.ld_reset_time.path()] = UiMqttConfig(
            [self.lockDetectDelayBox])
        # The output is the hold of the AOM lock, so "enabled" is the output low.
        settings_map[StabilizerSettings.aux_ttl_out.path()] = UiMqttConfig(
            [self.enableAOMLockBox], *invert)

        settings_map[UiSettings.lock_mode.path()] = UiMqttConfig(
            self.lock_mode_buttons(), *radio_group(LOCK_MODES))
        settings_map[UiSettings.relock.path()] = combine_configs({
            "wand_server":
            UiMqttConfig([self.wandServerEdit]),
            "wand_channel":
            UiMqttConfig([self.wandChannelEdit]),
            "solstis_host":
            UiMqttConfig([self.solstisHostEdit]),
        })

        self._settings_map = settings_map
        return settings_map

    def legacy_ui_map(self) -> dict:
        """The filter settings of the earlier `l674` UI (P/I gains, a notch): the fast PZT
        gains were negated when the filter was computed, so the `pid` filter gets the
        negated values; the integral gains are in Hz in both layouts."""
        fast, notch, slow = "ui/ch0/iir0", "ui/ch0/iir1", "ui/ch1/iir0"
        return {
            "ui/fast_gains/proportional": (fast, lambda v: {
                "filter": "pid",
                "pid/Kp": -v
            }),
            "ui/fast_gains/integral": (fast, lambda v: {
                "filter": "pid",
                "pid/Ki": -v
            }),
            "ui/fast_notch_enable": (notch, lambda v: {
                "filter": "notch" if v else "through"
            }),
            "ui/fast_notch/frequency": (notch, lambda v: {
                "notch/f0": v,
                "notch/K": 1.0
            }),
            "ui/fast_notch/quality_factor": (notch, lambda v: {
                "notch/Q": v
            }),
            "ui/slow_gains/proportional": (slow, lambda v: {
                "pid/Kp": v
            }),
            "ui/slow_gains/integral": (slow, lambda v: {
                "pid/Ki": v
            }),
            "ui/slow_enable": (slow, lambda v: {
                "filter": "pid" if v else "block"
            }),
        }

    def closeEvent(self, event):
        self.stop_lock_monitor()
        self.logView.close_handlers()
        super().closeEvent(event)
