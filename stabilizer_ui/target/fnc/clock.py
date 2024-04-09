import os
import logging
from PyQt5 import QtWidgets, uic

# Keep these up to date with the firmware
MIN_REFCLK_FREQUENCY_MHZ = 1
MIN_MULTIPLIED_REFCLK_FREQUENCY_MHZ = 10

REFCLK_MULTIPLIER_DISABLED = 1
MIN_REFCLK_ENABLED_MULTIPLIER = 4
MAX_REFCLK_MULTIPLIER = 20

HIGH_GAIN_VCO_RANGE_MHZ = (255, 500)
LOW_GAIN_VCO_RANGE_MHZ = (100, 160)

logger = logging.getLogger(__name__)


class ClockWidget(QtWidgets.QGroupBox):
    """Widget for setting the DDS reference clock"""

    def __init__(self):
        super().__init__()

        uic.loadUi(
            os.path.join(os.path.dirname(os.path.realpath(__file__)), "widgets/clock.ui"),
            self)

        self.refFrequencyBox.setMinimum(MIN_REFCLK_FREQUENCY_MHZ)
        self.refFrequencyBox.setMaximum(HIGH_GAIN_VCO_RANGE_MHZ[1])

        self.multiplierBox.setMinimum(1)
        self.multiplierBox.setMaximum(MAX_REFCLK_MULTIPLIER)

        # Store original values for hysteresis when jumping between multiplier enabled/disabled
        # or when validating frequency
        self._current_multiplier = self.multiplierBox.value()
        self._current_frequency = self.refFrequencyBox.value()

        # Validate frequency and multiplier to avoid panicking the firmware
        self.multiplierBox.valueChanged.connect(self._constrain_dds_ref_clk_multiplier)
        self.refFrequencyBox.valueChanged.connect(self._constrain_dds_ref_clk_frequency)

    def _constrain_dds_ref_clk_multiplier(self, multiplier):
        """Constrain the DDS reference clock multiplier to 1 (multiplier disabled) 
        or in the range 4-20 (inclusive), and validate the resulting PLL clock frequency """

        multiplier_enabled = bool(multiplier > 1)
        frequency = self.refFrequencyBox.value()

        # Jump multiplier between disabled and lowest enabled value
        if 1 < multiplier < MIN_REFCLK_ENABLED_MULTIPLIER:
            if self._current_multiplier == REFCLK_MULTIPLIER_DISABLED:
                multiplier = MIN_REFCLK_ENABLED_MULTIPLIER
            elif self._current_multiplier >= MIN_REFCLK_ENABLED_MULTIPLIER:
                multiplier = REFCLK_MULTIPLIER_DISABLED

        # Disable multiplier if reference clock is under MIN_MULTIPLIED_REFCLK_FREQUENCY_MHZ
        if multiplier_enabled and frequency < MIN_MULTIPLIED_REFCLK_FREQUENCY_MHZ:
            logger.warning(
                f"Cannot enable multiplier with clock frequency below \
                    {MIN_MULTIPLIED_REFCLK_FREQUENCY_MHZ:.2f} MHz"
            )
            self.multiplierBox.setValue(REFCLK_MULTIPLIER_DISABLED)
            return

        (multiplied_frequency,
         value_changed) = self._constrain_multiplied_frequency(multiplier, frequency)
        multiplier = int(multiplied_frequency // frequency)

        if value_changed:
            # The updated multiplier may be enabled below MIN_REFLCK_ENABLED_MULTIPLIER, re-check
            # If invalid again, refuse to update
            if 1 < multiplier < MIN_REFCLK_ENABLED_MULTIPLIER:
                multiplier = self._current_multiplier

            logger.warning(
                f"Invalid multiplier, setting to nearest valid value: {multiplier}")

        self.multiplierBox.setValue(multiplier)
        self._current_multiplier = multiplier

    def _constrain_dds_ref_clk_frequency(self, frequency):
        """Validate the PLL clock frequency"""

        multiplier = self.multiplierBox.value()
        multiplier_enabled = bool(multiplier > 1)

        satisfies_lower_bound = False

        # Check lower bounds of reference clock frequency
        if multiplier_enabled and frequency < MIN_MULTIPLIED_REFCLK_FREQUENCY_MHZ:
            frequency = MIN_MULTIPLIED_REFCLK_FREQUENCY_MHZ
            logger.warning(
                "Reference clock frequency too low for PLL with enabled multiplier")
        elif (not multiplier_enabled) and frequency < MIN_REFCLK_FREQUENCY_MHZ:
            frequency = MIN_REFCLK_FREQUENCY_MHZ
            logger.warning("Reference clock frequency too low for PLL")
            satisfies_lower_bound
        else:
            satisfies_lower_bound = True

        # Validate multiplied frequency
        (multiplied_frequency,
         value_changed) = self._constrain_multiplied_frequency(multiplier, frequency)
        value_changed = (not satisfies_lower_bound) or value_changed

        frequency = multiplied_frequency / multiplier

        if value_changed:
            logger.warning(
                f"Invalid frequency, setting to nearest valid value: {frequency:.2f} MHz"
            )

        self.refFrequencyBox.setValue(frequency)

    def _constrain_multiplied_frequency(self, multiplier, frequency):
        """Constrain the multilied PLL clock frequency to valid ranges"""
        multiplier_enabled = bool(multiplier > 1)
        multiplied_frequency = multiplier * frequency

        value_changed = True
        if multiplied_frequency > HIGH_GAIN_VCO_RANGE_MHZ[1]:
            multiplied_frequency = HIGH_GAIN_VCO_RANGE_MHZ[1]
            logger.warning("Reference clock frequency too high for PLL")
        # Lower limit
        elif multiplier_enabled and multiplied_frequency < LOW_GAIN_VCO_RANGE_MHZ[0]:
            multiplied_frequency = LOW_GAIN_VCO_RANGE_MHZ[0]
            logger.warning("Reference clock frequency too low for PLL")
        # In between low and high gain VCOs
        elif LOW_GAIN_VCO_RANGE_MHZ[1] < multiplied_frequency < HIGH_GAIN_VCO_RANGE_MHZ[0]:
            if self._current_frequency * self._current_multiplier <= LOW_GAIN_VCO_RANGE_MHZ[0]:
                multiplied_frequency = HIGH_GAIN_VCO_RANGE_MHZ[1]
            else:
                multiplied_frequency = LOW_GAIN_VCO_RANGE_MHZ[0]
                logger.warning("Multiplied frequency between VCO ranges")
        else:
            value_changed = False

        return (multiplied_frequency, value_changed)
