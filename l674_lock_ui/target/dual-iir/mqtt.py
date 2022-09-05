import asyncio
import logging
from enum import Enum, unique

from ...mqtt import StabilizerInterfaceBase
from ...iir import *

logger = logging.getLogger(__name__)


def _construct_settings_enum():
    channels = ['0', '1']
    iirs = ['0', '1']

    filters = zip(['pid', 'notch', 'lowpass', 'highpass', 'allpass'],
                  [PidArgs, NotchArgs, XPassArgs, XPassArgs, XPassArgs])

    settings = {}
    for c in channels:
        filter_settings[f"channel_{c}_afe_gain"] = f"settings/afe/{c}"
        for iir_idx in iirs:
            name_root = f"channel_{c}_iir_{iir_idx}_"
            path_root = f"ui/channel_{c}/iir_{iir_idx}/"
            filter_args[name_root + "filter"] = path_root + "filter"
            filter_args[name_root + "x_offset"] = path_root + "x_offset"
            filter_args[name_root + "y_offset"] = path_root + "y_offset"
            filter_args[name_root + "y_min"] = path_root + "y_min"
            filter_args[name_root + "y_max"] = path_root + "y_max"
            for f_str, f_args in filters:
                for arg in f_args.parameters:
                    filter_args[name_root +
                                f"{f_str}_{arg}"] = path_root + f"{f_str}/{arg}"
    settings['stream_target'] = "settings/stream_target"
    return Enum('DynamicEnum', settings)


Settings = _construct_settings_enum()


def _construct_iir_settings_dicts(channel, iir):
    _prefix = f"channel_{c}_iir_{iir}_"
    filters = zip(['pid', 'notch', 'lowpass', 'highpass', 'allpass'],
                  [PidArgs, NotchArgs, XPassArgs, XPassArgs, XPassArgs])
    settings = [_prefix + "filter"]
    for f_str, f_args in filters:
        for arg in f_args.parameters:
            settings.append(_prefix + f"{f_str}_{arg}")
    return {getattr(Settings, setting) for setting in settings}


def _construct_native_settings_dict():
    native_settings = ["stream_target"]
    _settings = ["afe_gain"]
    for channel in range(2):
        native_settings += f"channel_{c}_" + "afe_gain"
    return {getattr(Settings, setting) for setting in native_settings}


def _get_channel_and_iir(setting: Settings):
    setting_str = setting._name_
    _split_str = setting_str.split('_')
    return int(_split_str[1]), int(_split_str[3])


class StabilizerInterface(StabilizerInterfaceBase):
    """
    Shim for controlling `l674` stabilizer over MQTT; for the relock task to have something
    to hold on to before the Stabilizer write task has finished initial bringup of the
    MQTT connection. (Could potentially just use two MQTT connections instead, although
    that seems a bit wasteful.)
    """
    #: Settings which are directly addressed to Stabilizer.
    native_settings = _construct_native_settings_dict()
    #: IIR[0][0]
    channel_0_iir_0 = _construct_iir_settings_dicts(0, 0)
    #: IIR[0][1]
    channel_0_iir_1 = _construct_iir_settings_dicts(0, 1)
    #: IIR[1][0]
    channel_1_iir_0 = _construct_iir_settings_dicts(1, 0)
    #: IIR[1][1]
    channel_1_iir_1 = _construct_iir_settings_dicts(1, 1)

    iir_ch_topic_base = "settings/iir_ch"

    async def triage_setting_change(self, setting: Settings, all_values: Dict[Settings,
                                                                              Any]):
        if setting in self.native_settings:
            await self.request_settings_change(setting.value, all_values[setting])
        else:
            self.publish_ui_change(setting.value, all_values[setting])

        if (setting in self.channel_0_iir_0) or (setting in self.channel_0_iir_1) or (
                setting in self.channel_1_iir_0) or (setting in self.channel_1_iir_1):
            channel, iir = _get_channel_and_iir(setting)
            _prefix = f"channel_{channel}_iir_{iir}_"
            if all_values[getattr(Settings, _prefix + "filter")] == "pid":
                _prefix += "pid_"
                kwargs = {param: _prefix + param for param in PidArgs.parameters}
                ba = PIDFilter.get_ba(**kwargs)
            elif all_values[getattr(Settings, _prefix + "filter")] == "notch":
                _prefix += "notch_"
                kwargs = {param: _prefix + param for param in NotchArgs.parameters}
                ba = NotchFilter.get_ba(**kwargs)
            elif all_values[getattr(Settings, _prefix + "filter")] == "lowpass":
                _prefix += "lowpass_"
                kwargs = {param: _prefix + param for param in LowpassArgs.parameters}
                ba = LowpassFilter.get_ba(**kwargs)
            elif all_values[getattr(Settings, _prefix + "filter")] == "highpass":
                _prefix += "highpass_"
                kwargs = {param: _prefix + param for param in HighpassArgs.parameters}
                ba = HighpassFilter.get_ba(**kwargs)
            elif all_values[getattr(Settings, _prefix + "filter")] == "allpass":
                _prefix += "allpass_"
                kwargs = {param: _prefix + param for param in AllpassArgs.parameters}
                ba = AllpassFilter.get_ba(**kwargs)
            else:
                raise ValueError("Unknown filter set.")
            x_offset = all_values[getattr(Settings, _prefix + "x_offset")]
            y_offset = all_values[getattr(Settings, _prefix + "y_offset")]
            y_min = all_values[getattr(Settings, _prefix + "y_min")]
            y_max = all_values[getattr(Settings, _prefix + "y_max")]

            await self.set_iir(channel=channel,
                               iir_idx=iir,
                               ba=ba,
                               y_offset=y_offset,
                               y_min=y_min,
                               y_max=y_max)
