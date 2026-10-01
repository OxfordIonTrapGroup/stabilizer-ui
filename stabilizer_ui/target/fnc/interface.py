from stabilizer import DEFAULT_FNC_SAMPLE_PERIOD

from .topics import app_root
from ...interface import AbstractStabilizerInterface


class StabilizerInterface(AbstractStabilizerInterface):
    """
    Interface for the FNC stabilizer.
    """

    def __init__(self):
        super().__init__(DEFAULT_FNC_SAMPLE_PERIOD, app_root)
