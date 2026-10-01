import logging
from ...topic_tree import TopicTree
from ...iir.filters import FILTERS

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

        # feedforward_harmonics/0 is the fundamental; each is a single
        # `{"amp", "phase_turns"}` leaf
        cls.feedforward_harmonics = cls.root.create_child(
            "feedforward_harmonics").create_children(
                [str(harmonic) for harmonic in range(NUM_HARMONICS)])

        # Settling time exponents of the frequency (0) and phase (1) lock
        cls.pll_tcs = cls.root.create_child("pll_tc").create_children(["0", "1"])

        cls.frontend_offset = cls.root.create_child("frontend_offset")
        cls.feedback_offset = cls.root.create_child("feedback_offset")


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

        for ch in range(NUM_CHANNELS):
            for iir in range(NUM_IIR_FILTERS_PER_CHANNEL):
                cls.iirs[ch][iir].create_children(
                    ["filter", "y_offset", "y_min", "y_max", "x_offset"])

                for filter in FILTERS:
                    filter_topic = cls.iirs[ch][iir].create_child(filter.filter_type)
                    filter_topic.create_children(filter.parameters)


UiSettings.set()

global app_root
app_root = TopicTree.new("dt/sinara/current_sense/<MAC>")
app_root.set_children([StabilizerSettings.root, UiSettings.root])
app_root.create_children(["meta", "alive"])
app_root.set_app_root()
