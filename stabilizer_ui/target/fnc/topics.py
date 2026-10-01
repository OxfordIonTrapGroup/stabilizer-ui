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

        # Pounder settings
        pounder_channels = [channel.create_child("pounder") for channel in channels]

        for topic in [
                "frequency_dds_out", "frequency_dds_in", "amplitude_dds_out",
                "amplitude_dds_in", "attenuation_out", "attenuation_in"
        ]:
            setattr(cls, f"{topic}s",
                    [pounder_ch.create_child(topic) for pounder_ch in pounder_channels])


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
        cls.dds_io_link_checkboxes = [
            ui_channels[ch].create_child("dds_in_checkbox") for ch in range(NUM_CHANNELS)
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
app_root = TopicTree.new("dt/sinara/fnc/<MAC>")
app_root.set_children([StabilizerSettings.root, UiSettings.root])
app_root.create_children(["meta", "alive"])
app_root.set_app_root()
