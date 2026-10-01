import logging

from . import SAMPLE_PERIOD
from .topics import app_root
from ...interface import AbstractStabilizerInterface

logger = logging.getLogger(__name__)


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Shim for controlling `current_sense` stabilizer over MQTT
    """

    def __init__(self):
        super().__init__(SAMPLE_PERIOD, app_root)

    async def triage_setting_change(self, setting):
        logger.info(f"Changing setting {setting.path()}': {setting.value}")

        setting_root = setting.app_root()
        if setting_root.name == "settings":
            await self.request_settings_change(setting.path(), setting.value)
        elif setting_root.name == "ui":
            self.publish_ui_change(setting.path(), setting.value)

            if ui_iir := setting.get_parent_until(lambda x: x.name.startswith("iir")):
                await self._change_filter_setting(ui_iir)
