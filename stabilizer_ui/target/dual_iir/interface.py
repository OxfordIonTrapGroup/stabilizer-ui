from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD

from .topics import app_root
from ...interface import AbstractStabilizerInterface


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Shim for controlling `dual-iir` stabilizer over MQTT
    """

    def __init__(self):
        super().__init__(DEFAULT_DUAL_IIR_SAMPLE_PERIOD, app_root)
