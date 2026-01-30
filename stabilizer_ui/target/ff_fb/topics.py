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

        (afe, cls.iir_root) = cls.root.create_children(["afe", "iir_ch"])

        cls.iir_root.create_children(["0", "1"])

        cls.stream_target = cls.root.create_child("stream_target")
        cls.afes = afe.create_children(["0", "1"])

        # iir_ch/0/1 represents the IIR filter 1 for channel 0
        cls.iirs = [
            cls.iir_root.create_children(
                [f"{ch}/{iir}" for iir in range(NUM_IIR_FILTERS_PER_CHANNEL)])
            for ch in range(NUM_CHANNELS)
        ]


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
app_root = TopicTree.new("dt/sinara/dual-iir/<MAC>")
app_root.set_children([StabilizerSettings.root, UiSettings.root])
app_root.create_children(["meta", "alive"])
app_root.set_app_root()
