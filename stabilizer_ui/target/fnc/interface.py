import logging

from ...mqtt import StabilizerInterface
from ...iir.filters import FILTERS

logger = logging.getLogger(__name__)


class FncInterface(StabilizerInterface):
    """
    Interface for the FNC stabilizer.
    """

    iir_ch_topic_base = "settings/iir_ch"

    async def triage_setting_change(self, setting):
        logger.info("Change setting {root}'")

        setting_root = setting.root()
        if setting_root.name == "settings":
            await self.request_settings_change(setting.get_path_from_root(),
                                               setting.get_message())
        elif setting_root.name == "ui":
            self.publish_ui_change(setting.get_path_from_root(), setting.get_message())
            ui_iir = setting.get_parent_until(lambda x: x.name.startswith("iir"))

            (_ch, _iir_idx) = int(ui_iir.get_parent().name[2:]), int(ui_iir.name[3:])

            filter_type = ui_iir.get_child("filter").get_message()
            filter = ui_iir.get_child(filter_type)

            # require parameters to be set by application
            if not filter.has_children():
                raise ValueError(f"Filter {filter_type} parameter messsages not created.")
            filter_params = {f.name: f.get_message() for f in filter.get_children()}

            ba = next(f for f in FILTERS
                      if f.filter_type == filter_type).get_coefficients(**filter_params)

            await self.set_iir(
                channel=_ch,
                iir_idx=_iir_idx,
                ba=ba,
                y_offset=ui_iir.get_child("y_offset").get_message(),
                y_min=ui_iir.get_child("y_min").get_message(),
                y_max=ui_iir.get_child("y_max").get_message(),
            )
