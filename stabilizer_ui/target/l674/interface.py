import logging

import stabilizer

from . import SAMPLE_PERIOD
from .topics import app_root
from ...interface import AbstractStabilizerInterface

logger = logging.getLogger(__name__)

#: The PZT lock modes (the value of `ui/lock_mode`), in the order of the radio buttons.
LOCK_MODES = ["Disabled", "RampPassThrough", "Enabled"]

#: Gain of the fast PZT channel in the `RampPassThrough` mode: gives approximately ±10 V
#: when driven using the Vescent servo box ramp.
RAMP_PASS_THROUGH_GAIN = 5.0

LOCK_MODE_KEY = "ui/lock_mode"
ADC1_FILTERED_KEY = "settings/lock_detect/adc1_filtered"

#: The biquads the lock mode overrides (the first of each channel), by their key on the
#: device.
LOCK_MODE_BIQUADS = [f"settings/ch/{ch}/biquad/0/repr/Raw" for ch in range(2)]


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Shim for controlling the `l674` stabilizer over MQTT.

    The lock mode (`ui/lock_mode`) decides what the first biquad of each channel is: zero
    (`Disabled`), a fixed gain on the fast PZT channel for the Vescent ramp
    (`RampPassThrough`), or the filter designed in the UI (`Enabled`). The other biquads
    (the notch on the fast channel, and the second one of the slow channel) are always as
    designed. The designed filters stay in the UI state, so that they can be edited while
    the lock is off.
    """

    def __init__(self):
        super().__init__(SAMPLE_PERIOD, app_root)

    def lock_mode(self) -> str:
        """The current lock mode (as shown in the UI)."""
        value = self.app_root.child(LOCK_MODE_KEY).value
        return value if value in LOCK_MODES else LOCK_MODES[0]

    def _biquad_value(self, iir_setting) -> dict:
        ch, idx = int(iir_setting.get_parent().name[2:]), int(iir_setting.name[3:])
        mode = self.lock_mode()
        if idx != 0 or mode == "Enabled":
            return super()._biquad_value(iir_setting)
        gain = RAMP_PASS_THROUGH_GAIN if (mode == "RampPassThrough" and ch == 0) else 0.0
        settings = iir_setting.value
        return {
            "coeff": {
                "ba": [gain, 0.0, 0.0, 0.0, 0.0]
            },
            "u": 0,
            "min": stabilizer.voltage_to_machine_units(settings["y_min"]),
            "max": stabilizer.voltage_to_machine_units(settings["y_max"]),
        }

    async def change(self, setting):
        await super().change(setting)
        if setting.path() == LOCK_MODE_KEY:
            # The mode decides the coefficients of the first biquads.
            for raw_key in LOCK_MODE_BIQUADS:
                await self._change_filter_setting(self._iirs[raw_key])

    def _ui_value_received(self, key: str):
        super()._ui_value_received(key)
        if key == LOCK_MODE_KEY:
            self._update_all_topics()
            for raw_key in LOCK_MODE_BIQUADS:
                self._check_biquad(raw_key)

    async def read_transmission(self) -> float:
        """The low-pass filtered cavity transmission, in volts at the ADC1 input (raising
        `ConnectionError` if the device is not connected)."""
        return float(await self.get_setting(ADC1_FILTERED_KEY))
