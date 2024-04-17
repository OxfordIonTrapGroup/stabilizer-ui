import logging
from enum import Enum, unique

import numpy as np
from stabilizer import SAMPLE_PERIOD

from ...mqtt import AbstractStabilizerInterface

logger = logging.getLogger(__name__)


@unique
class Settings(Enum):
    fast_p_gain = "ui/fast_gains/proportional"
    fast_i_gain = "ui/fast_gains/integral"
    fast_notch_enable = "ui/fast_notch_enable"
    fast_notch_frequency = "ui/fast_notch/frequency"
    fast_notch_quality_factor = "ui/fast_notch/quality_factor"
    slow_p_gain = "ui/slow_gains/proportional"
    slow_i_gain = "ui/slow_gains/integral"
    slow_enable = "ui/slow_enable"
    lock_mode = "ui/lock_mode"
    gain_ramp_time = "settings/gain_ramp_time"
    ld_threshold = "settings/ld_threshold"
    ld_reset_time = "settings/ld_reset_time"
    adc1_routing = "settings/adc1_routing"
    aux_ttl_out = "settings/aux_ttl_out"
    stream_target = "settings/stream_target"
    afe0_gain = "settings/afe/0"
    afe1_gain = "settings/afe/1"


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Shim for controlling `l674` stabilizer over MQTT; for the relock task to have
    something to hold on to before the Stabilizer write task has finished initial
    bringup of the MQTT connection. (Could potentially just use two MQTT connections
    instead, although that seems a bit wasteful.)
    """
    #: Settings which are directly addressed to Stabilizer.
    native_settings = {
        Settings.gain_ramp_time,
        Settings.ld_threshold,
        Settings.ld_reset_time,
        Settings.adc1_routing,
        Settings.aux_ttl_out,
        Settings.stream_target,
        Settings.afe0_gain,
        Settings.afe1_gain,
    }
    #: IIR[0][0]
    fast_pid_settings = {
        Settings.fast_p_gain,
        Settings.fast_i_gain,
    }
    #: IIR[0][1]
    fast_notch_settings = {
        Settings.fast_notch_enable,
        Settings.fast_notch_frequency,
        Settings.fast_notch_quality_factor,
    }
    #: IIR[1][0]
    slow_pid_settings = {
        Settings.slow_p_gain,
        Settings.slow_i_gain,
        Settings.slow_enable,
    }
    read_adc1_filtered_topic = "read_adc1_filtered"
    iir_ch_topic_base = "settings/iir_ch"

    async def read_adc(self) -> float:
        await self._interface_set.wait()
        # Argument irrelevant.
        return float(await self._interface.request(self.read_adc1_filtered_topic, 0))

    async def triage_setting_change(self, setting, all_values):
        if setting in self.native_settings:
            await self.request_settings_change(setting.value, all_values[setting])
        else:
            self.publish_ui_change(setting.value, all_values[setting])

        if (setting is Settings.lock_mode or setting in self.fast_pid_settings
                or setting in self.slow_pid_settings):
            lock_mode = all_values[Settings.lock_mode]
            if lock_mode != "Enabled" and setting is not Settings.lock_mode:
                # Ignore gain changes if lock is not enabled.
                pass
            elif lock_mode == "Disabled":
                await self.set_iir(channel=0, iir_idx=0, ba=[0.0] * 5)
                await self.set_iir(channel=1, iir_idx=0, ba=[0.0] * 5)
            elif lock_mode == "RampPassThrough":
                # Gain 5 gives approx ±10V when driven using the Vescent servo box ramp.
                await self.set_iir(channel=0, iir_idx=0, ba=[5.0] + [0.0] * 4)
                await self.set_iir(channel=1, iir_idx=0, ba=[0.0] * 5)
            else:
                # Negative sign in fast branch to match AOM lock; both PZTs have same sign
                await self.set_pi_gains(channel=0,
                                        iir_idx=0,
                                        p_gain=-all_values[Settings.fast_p_gain],
                                        i_gain=-all_values[Settings.fast_i_gain])
                if all_values[Settings.slow_enable]:
                    await self.set_pi_gains(channel=1,
                                            iir_idx=0,
                                            p_gain=all_values[Settings.slow_p_gain],
                                            i_gain=all_values[Settings.slow_i_gain])
                else:
                    await self.set_pi_gains(channel=1, iir_idx=0, p_gain=0.0, i_gain=0.0)

        if setting in self.fast_notch_settings:
            if all_values[Settings.fast_notch_enable]:
                f0 = (all_values[Settings.fast_notch_frequency] * np.pi * SAMPLE_PERIOD)
                q = all_values[Settings.fast_notch_quality_factor]
                # unit gain
                denominator = (1 + f0 / q + f0**2)
                a1 = 2 * (1 - f0**2) / denominator
                a2 = -(1 - f0 / q + f0**2) / denominator
                b0 = (1 + f0**2) / denominator
                b1 = -(2 * (1 - f0**2)) / denominator
                await self.set_iir(channel=0, iir_idx=1, ba=[b0, b1, b0, a1, a2])
            else:
                await self.set_iir(channel=0, iir_idx=1, ba=[1.0] + [0.0] * 4)

        # We rely on hardware to initialise IIR[1][1] with a simple pass-through response.
