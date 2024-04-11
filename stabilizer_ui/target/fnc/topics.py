import logging
from ...topic_tree import TopicTree
from ...iir.filters import FILTERS

from .parameters import *

logger = logging.getLogger(__name__)

# _stabilizer_settings = TopicTree("settings")
# _ui_settings = TopicTree("ui")

# # Create stabilizer settings topics tree
# _stream_target, _afe, _iir, _pounder, _dds_ref_clock = _stabilizer_settings.create_children(
#     ["stream_target", "afe", "iir_ch", "pounder", "dds_ref_clock"])

# _afe.create_children(["0", "1"])

# # iir_ch/0/1 represents the IIR filter 1 for channel 0
# for ch in _iir.create_children(["0", "1"]):
#     ch.create_children(["0", "1"])

# _ext_clk, _ref_clk_frequency, _clk_multiplier = _dds_ref_clock.create_children(
#     ["external_clock", "reference_clock_frequency", "multiplier"])

# # Pounder settings
# _pounder_channels = _pounder.create_children(["0", "1"])

# for ch in _pounder_channels:
#     ch.create_children([
#         "frequency_dds_out", "frequency_dds_in", "amplitude_dds_out", "amplitude_dds_in",
#         "attenuation_out", "attenuation_in"
#     ])

# # Create UI settings topics tree
# _ui_channels = _ui_settings.create_children(["ch0", "ch1"])
# for ch in _ui_channels:
#     for iir in ch.create_children(["iir0", "iir1"]):
#         iir.create_children(["filter", "y_offset", "y_min", "y_max", "x_offset"])
#         for filter in FILTERS:
#             filter_topic = iir.create_child(filter.filter_type)
#             filter_topic.create_children(filter.parameters)


class Stabilizer:
    """Enum wrapping the stabilizer settings topics tree.
    Topics in an array have separate entries for the parent topic and the subtopics
    """

    def __init__(self):
        self.root = TopicTree("settings")

        (afe, self.iir_root, self.pounder, self.dds_ref_clock) = self.root.create_children(
            ["afe", "iir_ch", "pounder", "dds_ref_clock"])

        self.iir_root.create_children(["0", "1"])

        self.stream_target = self.root.create_child("stream_target")
        self.afes = afe.create_children(["0", "1"])
        self.ext_clk, self.ref_clk_frequency, self.clk_multiplier = self.dds_ref_clock.create_children(
            ["external_clock", "reference_clock_frequency", "multiplier"])

        # iir_ch/0/1 represents the IIR filter 1 for channel 0
        self.iirs = [self.iir_root.create_children([f"{ch}/{iir}" for iir in range(NUM_IIR_FILTERS_PER_CHANNEL)]) for ch in range(NUM_CHANNELS)]                

        # Pounder settings
        pounder_channels = self.pounder.create_children(["0", "1"])

        for topic in [
                "frequency_dds_out", "frequency_dds_in", "amplitude_dds_out",
                "amplitude_dds_in", "attenuation_out", "attenuation_in"
        ]:
            setattr(self, f"{topic}s",
                    [pounder.create_child(topic) for pounder in pounder_channels])


global stabilizer
stabilizer = Stabilizer()


class Ui:
    """Enum wrapping the UI settings topics tree.
    """

    def __init__(self):
        self.root = TopicTree("ui")

        ui_channels = self.root.create_children([f"ch{ch}" for ch in range(NUM_CHANNELS)])
        self.iirs = [
            ui_channels[ch].create_children([f"iir{iir}" for iir in range(NUM_IIR_FILTERS_PER_CHANNEL)]) for ch in range(NUM_CHANNELS)
        ]

        for ch in range(NUM_CHANNELS):
            for iir in range(NUM_IIR_FILTERS_PER_CHANNEL):
                self.iirs[ch][iir].create_children(["filter", "y_offset", "y_min", "y_max", "x_offset"])

                for filter in FILTERS:
                    filter_topic = self.iirs[ch][iir].create_child(filter.filter_type)
                    filter_topic.create_children(filter.parameters)


global ui
ui = Ui()

# Root topic. Not using the method `add_child` because it sets the parent of the child
# It's unnecessary to see the sinara root when traversed up the tree when getting the
# path but provides a useful starting point to get all topics when traversing down.
# The MAC address to the Stabilizer board needs to be changed when the app is launched
global app_root
app_root = TopicTree.new("dt/sinara/fnc/<MAC>")
app_root._children = [stabilizer.root, ui.root]
