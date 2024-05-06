import logging
import os
from PyQt5 import QtWidgets, uic
from math import inf
from stabilizer import DEFAULT_FNC_SAMPLE_PERIOD
from stabilizer.stream import Parser, AdcDecoder, PhaseOffsetDecoder

from .topics import StabilizerSettings, UiSettings
from . import *

from ...pounder.ui import ClockWidget
from ...stream.fft_scope import FftScope
from ...mqtt import UiMqttConfig, NetworkAddress
from ...iir.filters import FILTERS, get_filter
from ...iir.channel_settings import AbstractChannelSettings
from ...ui import AbstractUiWindow
from ...utils import kilo, kilo2, mega, link_spinbox_to_is_inf_checkbox

logger = logging.getLogger(__name__)

#
# Parameters for the FNC ui.
#

DEFAULT_WINDOW_SIZE = (1400, 600)
DEFAULT_PHASE_PLOT_YRANGE = (0, 1)
DEFAULT_ADC_PLOT_YRANGE = (-1, 1)

# Default conversion from ADC voltage to phase turns
DEFAULT_ADC_VOLT_PHASE_SCALE = 0.2

#: Interval between scope plot updates, in seconds.
#: PyQt's drawing speed limits value.
SCOPE_UPDATE_PERIOD = 0.05  # 20 fps


class ChannelSettings(AbstractChannelSettings):
    """ Channel settings"""

    def __init__(self, sample_period=DEFAULT_FNC_SAMPLE_PERIOD):
        super().__init__()

        uic.loadUi(
            os.path.join(os.path.dirname(os.path.realpath(__file__)),
                         "widgets/channel.ui"), self)

        self._add_afe_options()
        self._add_iir_tabWidget(sample_period)

        # Disable mouse wheel scrolling on spinboxes to prevent accidental changes
        spinboxes = self.findChildren(QtWidgets.QDoubleSpinBox)
        for box in spinboxes:
            box.wheelEvent = lambda *event: None

        # Checkbox to fix DDS In frequency to 2x DDS Out (double pass AOM)
        self.ddsIoFreqLinkCheckBox.stateChanged.connect(self._linkDdsIoFrequencies)

        # Toggle CheckBox state to trigger initial link and frequency calculation
        self.ddsIoFreqLinkCheckBox.setChecked(False)
        self.ddsIoFreqLinkCheckBox.setChecked(True)

        # Snap attenuation values to 0.5 dB steps
        self.ddsInAttenuationBox.valueChanged.connect(self._snapAttenuationValue)
        self.ddsOutAttenuationBox.valueChanged.connect(self._snapAttenuationValue)

    def _linkDdsIoFrequencies(self, _):
        """Link DDS In frequency to 2x DDS Out frequency if enabled, otherwise allow
        manual setting. Attached to the stateChanged signal of the ddsIoFreqLinkCheckBox.
        """
        if self.ddsIoFreqLinkCheckBox.isChecked():
            # Disable DDS In frequency and set to 2x DDS Out frequency
            self.ddsInFrequencyBox.setValue(2 * self.ddsOutFrequencyBox.value())
            self.ddsInFrequencyBox.setEnabled(False)

            # Update DDS In frequency when DDS Out frequency changes
            self.ddsOutFrequencyBox.valueChanged.connect(self._updateDdsIoFrequencies)
        else:
            self.ddsInFrequencyBox.setEnabled(True)

            # If the method is not already connected, disconnect raises a TypeError
            try:
                self.ddsOutFrequencyBox.valueChanged.disconnect(
                    self._updateDdsIoFrequencies)
            except TypeError:
                pass

    def _updateDdsIoFrequencies(self, _):
        """Update DDS In frequency to when DDS Out frequency changes"""
        if self.ddsIoFreqLinkCheckBox.isChecked():
            self.ddsInFrequencyBox.setValue(2 * self.ddsOutFrequencyBox.value())

    def _snapAttenuationValue(self, value):
        """Snap attenuation values to 0.5 dB steps"""
        self.sender().setValue(0.5 * round(2 * value))


class ChannelTabWidget(QtWidgets.QTabWidget):
    """ Channel tab widget comprising NUM_CHANNELS ChannelSettings tabs"""

    def __init__(self):
        super().__init__()

        self.channels = [ChannelSettings() for _ in range(NUM_CHANNELS)]
        for i in range(NUM_CHANNELS):
            self.addTab(self.channels[i], f"Channel {i}")


class UiWindow(AbstractUiWindow):
    """ Main UI window for FNC"""

    def __init__(self, title: str = "FNC"):
        super().__init__()
        self.setWindowTitle(title)

        # Set up main window with Horizontal layout
        self.setCentralWidget(QtWidgets.QWidget(self))
        centralLayout = QtWidgets.QHBoxLayout(self.centralWidget())

        # Add FFT scope next to a vertical layout for settings
        settingsLayout = QtWidgets.QVBoxLayout()

        streamParser = Parser([AdcDecoder(), PhaseOffsetDecoder()])
        self.fftScopeWidget = FftScope(streamParser, DEFAULT_FNC_SAMPLE_PERIOD)
        centralLayout.addLayout(settingsLayout)
        centralLayout.addWidget(self.fftScopeWidget)

        self.channelTabWidget = ChannelTabWidget()
        self.channels = self.channelTabWidget.channels
        self.clockWidget = ClockWidget()

        # As of 23/04/2024, updating the clock at runtime causes timing issues.
        # Disable the clock widget until this is resolved.
        self.clockWidget.setEnabled(False)

        settingsLayout.addWidget(self.clockWidget)
        settingsLayout.addWidget(self.channelTabWidget)

        # Set FFT scope to take max available space
        centralLayout.setStretchFactor(self.fftScopeWidget, 1)
        fftScopeSizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                                   QtWidgets.QSizePolicy.Expanding)
        self.fftScopeWidget.setSizePolicy(fftScopeSizePolicy)
        self.fftScopeWidget.setMinimumSize(400, 200)

        # Rescale axes and add an axis converting ADC voltage to phase
        for i in range(NUM_CHANNELS):
            adcPlotItem = self.fftScopeWidget.graphics_view.getItem(0, i)
            adcPlotItem.showAxis("right")
            adcPlotItem.setYRange(*DEFAULT_ADC_PLOT_YRANGE)

            adcRightAxis = adcPlotItem.getAxis("right")
            adcRightAxis.setScale(DEFAULT_ADC_VOLT_PHASE_SCALE)
            adcRightAxis.setLabel("Phase / turns")

            self.fftScopeWidget.graphics_view.getItem(
                1, i).setYRange(*DEFAULT_PHASE_PLOT_YRANGE)

        self.resize(*DEFAULT_WINDOW_SIZE)

    def update_stream(self, payload):
        self.fftScopeWidget.update(payload)

    def set_mqtt_configs(self, stream_target: NetworkAddress):
        """ Link the UI widgets to the MQTT topic tree"""

        settings_map = {}

        # `ui/#` are only used by the UI, the others by both UI and stabilizer
        settings_map[StabilizerSettings.stream_target.path()] = UiMqttConfig(
            [],
            lambda _: stream_target._asdict(),
            lambda _w, _v: stream_target._asdict(),
        )

        settings_map[StabilizerSettings.ext_clk.path()] = UiMqttConfig(
            [self.clockWidget.extClkCheckBox])
        settings_map[StabilizerSettings.ref_clk_frequency.path()] = UiMqttConfig(
            [self.clockWidget.refFrequencyBox], *mega)
        settings_map[StabilizerSettings.clk_multiplier.path()] = UiMqttConfig(
            [self.clockWidget.multiplierBox])

        for ch in range(NUM_CHANNELS):
            settings_map[StabilizerSettings.afes[ch].path()] = UiMqttConfig(
                [self.channels[ch].afeGainBox])

            settings_map[StabilizerSettings.attenuation_ins[ch].path()] = UiMqttConfig(
                [self.channels[ch].ddsInAttenuationBox])
            settings_map[StabilizerSettings.attenuation_outs[ch].path()] = UiMqttConfig(
                [self.channels[ch].ddsOutAttenuationBox])

            settings_map[StabilizerSettings.amplitude_dds_ins[ch].path()] = UiMqttConfig(
                [self.channels[ch].ddsInAmplitudeBox])
            settings_map[StabilizerSettings.amplitude_dds_outs[ch].path()] = UiMqttConfig(
                [self.channels[ch].ddsOutAmplitudeBox])

            settings_map[StabilizerSettings.frequency_dds_outs[ch].path()] = UiMqttConfig(
                [self.channels[ch].ddsOutFrequencyBox], *mega)
            settings_map[StabilizerSettings.frequency_dds_ins[ch].path()] = UiMqttConfig(
                [self.channels[ch].ddsInFrequencyBox], *mega)

            settings_map[UiSettings.dds_io_link_checkboxes[ch].path()] = UiMqttConfig(
                [self.channels[ch].ddsIoFreqLinkCheckBox])

            # IIR settings
            for iir in range(NUM_IIR_FILTERS_PER_CHANNEL):
                iirWidget = self.channels[ch].iir_widgets[iir]
                iir_topic = UiSettings.iirs[ch][iir]

                iirWidget.set_mqtt_configs(settings_map, iir_topic)

        return settings_map
