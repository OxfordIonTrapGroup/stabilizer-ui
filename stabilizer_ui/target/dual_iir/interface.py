import logging
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD

from .topics import app_root, StabilizerSettings
from ...interface import AbstractStabilizerInterface
from ...iir.filters import get_filter

logger = logging.getLogger(__name__)


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Shim for controlling `dual-iir` stabilizer over MQTT
    """

    iir_ch_topic_base = StabilizerSettings.iir_root.path()

    def __init__(self):
        super().__init__(DEFAULT_DUAL_IIR_SAMPLE_PERIOD, app_root)
        self.stream_target_topic = StabilizerSettings.stream_target.path(from_app_root=False)

    async def triage_setting_change(self, setting):
        logger.info(f"Changing setting {setting.path()}': {setting.value}")

        setting_root = setting.app_root()
        if setting_root.name == "settings":
            await self.request_settings_change(setting.path(),
                                               setting.value)
        elif setting_root.name == "ui":
            self.publish_ui_change(setting.path(), setting.value)

            if (ui_iir := setting.get_parent_until(lambda x: x.name.startswith("iir"))
                ) is not None:
                await self._change_filter_setting(ui_iir)