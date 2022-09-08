import asyncio
import logging
from enum import Enum, unique

from ...mqtt import StabilizerInterfaceBase
from ...iir import FILTERS

logger = logging.getLogger(__name__)


class StabilizerInterface(StabilizerInterfaceBase):
    """
    Shim for controlling `l674` stabilizer over MQTT; for the relock task to have something
    to hold on to before the Stabilizer write task has finished initial bringup of the
    MQTT connection. (Could potentially just use two MQTT connections instead, although
    that seems a bit wasteful.)
    """
    iir_ch_topic_base = "settings/iir_ch"

    async def triage_setting_change(self, setting, all_values):
        logger.info("Setting change'%s'", setting)
        if setting.split('/')[0] == 'settings':
            await self.request_settings_change(setting, all_values[setting])
        else:
            self.publish_ui_change(setting, all_values[setting])
            channel, iir = setting.split('/')[1:3]
            path_root = f"ui/{channel}/{iir}/"
            x_offset = all_values[path_root + "x_offset"]
            y_offset = all_values[path_root + "y_offset"]
            y_min = all_values[path_root + "y_min"]
            y_max = all_values[path_root + "y_max"]

            filter_type = all_values[path_root + "filter"]
            filter_idx = [f.filter_type for f in FILTERS].index(filter_type)
            kwargs = {
                        param: all_values[path_root + f"{filter_type}/{param}"]
                        for param in FILTERS[filter_idx].parameters
                    }
            ba = FILTERS[filter_idx].get_coefficients(**kwargs)

            await self.set_iir(channel=int(channel),
                               iir_idx=int(iir),
                               ba=ba,
                               y_offset=y_offset,
                               y_min=y_min,
                               y_max=y_max)
