from . import SAMPLE_PERIOD
from .topics import app_root
from ...interface import AbstractStabilizerInterface


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Shim for controlling `current_sense` stabilizer over MQTT
    """

    def __init__(self):
        super().__init__(SAMPLE_PERIOD, app_root)
