import logging
from ...topic_tree import TopicTree

from . import *

logger = logging.getLogger(__name__)


class StabilizerSettings:
    """Enum wrapping the stabilizer settings topics tree.
    Topics in an array have separate entries for the parent topic and the subtopics
    """

    @classmethod
    def set(cls):
        cls.root = TopicTree("settings")

        cls.stream = cls.root.create_child("stream")
        cls.trigger = cls.root.create_child("trigger")

        channels = cls.root.create_child("ch").create_children(
            [str(ch) for ch in range(NUM_CHANNELS)])
        cls.afes = [channel.create_child("gain") for channel in channels]
        cls.runs = [channel.create_child("run") for channel in channels]

        # ch/0/biquad/1 represents the IIR filter 1 for channel 0
        cls.iirs = [
            channel.create_child("biquad").create_children(
                [str(iir) for iir in range(NUM_IIR_FILTERS_PER_CHANNEL)])
            for channel in channels
        ]

        # Ramp time of the error signal gain after the lock is switched on
        cls.gain_ramp_time = cls.root.create_child("gain_ramp_time")
        # Lock detection on the cavity transmission (ADC1)
        lock_detect = cls.root.create_child("lock_detect")
        cls.ld_threshold = lock_detect.create_child("threshold")
        cls.ld_reset_time = lock_detect.create_child("reset_time")
        # The filtered transmission (read-only)
        cls.adc1_filtered = lock_detect.create_child("adc1_filtered")
        # The auxiliary TTL output (the hold of the AOM lock)
        cls.aux_ttl_out = cls.root.create_child("aux_ttl_out")


StabilizerSettings.set()


class UiSettings:
    """Enum wrapping the UI settings topics tree.
    """

    @classmethod
    def set(cls):
        cls.root = TopicTree("ui")

        ui_channels = cls.root.create_children([f"ch{ch}" for ch in range(NUM_CHANNELS)])
        cls.iirs = [
            ui_channels[ch].create_children(
                [f"iir{iir}" for iir in range(NUM_IIR_FILTERS_PER_CHANNEL)])
            for ch in range(NUM_CHANNELS)
        ]
        # The PZT lock mode (`Disabled`, `RampPassThrough`, `Enabled`), which decides what
        # is written for the first biquad of each channel.
        cls.lock_mode = cls.root.create_child("lock_mode")
        # The relocking parameters (WAnD server and channel, SolsTiS host)
        cls.relock = cls.root.create_child("relock")


UiSettings.set()

global app_root
app_root = TopicTree.new("dt/sinara/l674/<MAC>")
app_root.set_children([StabilizerSettings.root, UiSettings.root])
app_root.create_children(["meta", "alive"])
app_root.set_app_root()
