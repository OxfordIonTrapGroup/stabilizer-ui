import os

from ...iir.channel_settings import AbstractChannelSettings
from PyQt5 import QtWidgets, uic

NUM_CHANNELS = 2

class ChannelSettings(AbstractChannelSettings):
    """ Channel settings"""
    def __init__(self):
        super().__init__()

        uic.loadUi(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               "widgets/channel.ui"), self)
        
        self._add_afe_options()
        self._add_iir_tabWidget()
        
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
        """Link DDS In frequency to 2x DDS Out frequency if enabled, otherwise allow manual setting.
        Attached to the stateChanged signal of the ddsIoFreqLinkCheckBox.
        """
        if self.ddsIoFreqLinkCheckBox.isChecked():
            # Disable DDS In frequency and set to 2x DDS Out frequency
            self.ddsInFrequencyBox.setValue(2 * self.ddsOutFrequencyBox.value())
            self.ddsInFrequencyBox.setEnabled(False)

            # Update DDS In frequency when DDS Out frequency changes
            self.ddsOutFrequencyBox.valueChanged.connect(
                self._updateDdsIoFrequencies)
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
            self.addTab(ChannelSettings(), f"Channel {i}")

        