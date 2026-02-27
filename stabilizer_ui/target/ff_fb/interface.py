import logging
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD

from .topics import app_root, StabilizerSettings
from ...interface import AbstractStabilizerInterface

logger = logging.getLogger(__name__)


class StabilizerInterface(AbstractStabilizerInterface):
    """
    MQTT interface layer for the ff_fb (dual-iir + feedforward) stabilizer.

    Responsibilities:
    - Bridge UI settings tree to firmware MQTT topics
    - Route filter and harmonic parameter updates
    - Handle streaming target configuration
    - Maintain sample period context for coefficient generation
    """
    
    # Base MQTT topic paths for:
    # - IIR filter settings
    # - Harmonic feedforward parameters
    iir_ch_topic_base = StabilizerSettings.iir_root.path()
    hparam_topic_base = StabilizerSettings.harm_param_root.path()
    

    def __init__(self):
        """
        Initialize interface with:
        - Default sample period (used for coefficient calculations)
        - Application topic root
        """
        super().__init__(DEFAULT_DUAL_IIR_SAMPLE_PERIOD, app_root)
        # Topic used to configure UDP stream target on firmware
        self.stream_target_topic = StabilizerSettings.stream_target.path(
            from_app_root=False)

    async def triage_setting_change(self, setting):
        """
        Route a setting change originating from the UI tree.

        Behaviour:
        - If under `settings/`, forward directly to firmware.
        - If under `ui/`, publish UI state and determine whether
          a filter or harmonic update must be computed.
        """
        logger.info(f"Changing setting {setting.path()}': {setting.value}")
        
        # Direct firmware settings (no local computation required)
        setting_root = setting.app_root()
        if setting_root.name == "settings":
            await self.request_settings_change(setting.path(), setting.value)
        # UI-side settings (may require coefficient generation first)
        elif setting_root.name == "ui":
            self.publish_ui_change(setting.path(), setting.value)
            
            # If change belongs to an IIR filter subtree,
            # recompute and push updated coefficients.
            if ui_iir := setting.get_parent_until(lambda x: x.name.startswith("iir")):
                await self._change_filter_setting(ui_iir)
                
            # If change belongs to harmonic parameters subtree,
            # recompute and push harmonic amplitude/phase update.
            elif ui_harm := setting.get_parent_until(lambda x: x.name.startswith("h_params")):
                await self._change_harmonic_settings(ui_harm)

