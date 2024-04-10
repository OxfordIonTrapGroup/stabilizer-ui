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

        (afe, self.iir_root, pounder, dds_ref_clock) = self.root.create_children(
            ["afe", "iir_ch", "pounder", "dds_ref_clock"])

        self.iir_root.create_children(["0", "1"])

        self.stream_target = self.root.create_child("stream_target")
        self.afes = afe.create_children(["0", "1"])
        self.ext_clk, self.ref_clk_frequency, self.clk_multiplier = dds_ref_clock.create_children(
            ["external_clock", "reference_clock_frequency", "multiplier"])

        # iir_ch/0/1 represents the IIR filter 1 for channel 0
        self.iirs = [
            self.iir_root.children[i].create_children(["0", "1"])
            for i in range(NUM_IIR_FILTERS_PER_CHANNEL)
        ]

        # Pounder settings
        pounder_channels = pounder.create_children(["0", "1"])

        for topic in [
                "frequency_dds_out", "frequency_dds_in", "amplitude_dds_out",
                "amplitude_dds_in", "attenuation_out", "attenuation_in"
        ]:
            setattr(self, f"{topic}s",
                    [pounder.create_child(topic) for pounder in pounder_channels])

        # self.frequency_dds_outs = [pounder.create_child("frequency_dds_out") for pounder in pounder_channels]
        # self.frequency_dds_ins = [pounder.create_child("frequency_dds_in") for pounder in pounder_channels]
        # self.attenuation_outs = [pounder.create_child("attenuation_out") for pounder in pounder_channels]
        # self.attenuation_ins = [pounder.create_child("attenuation_in") for pounder in pounder_channels]
        # self.amplitude_dds_outs = [pounder.create_child("amplitude_dds_out") for pounder in pounder_channels]
        # self.amplitude_dds_ins = [pounder.create_child("amplitude_dds_in") for pounder in pounder_channels]

        # root = _stabilizer_settings
        # stream_target = _stream_target

        # # IIR parent topic group and subtopics
        # # Access subtopics as iirs[ch_idx][iir_idx]
        # iir = _iir
        # iirs = [channel.get_children() for channel in _iir.get_children()]

        # # AFE parent topic group and subtopics for each channel
        # afe = _afe
        # afes = _afe.get_children()

        # # DDS reference clock
        # clock = _dds_ref_clock
        # ext_clk = _ext_clk
        # ref_clk_frequency = _ref_clk_frequency
        # clk_multiplier = _clk_multiplier

        # # Pounder topic
        # pounder = _pounder
        # pounder_channels = _pounder_channels


global stabilizer
stabilizer = Stabilizer()


class Filter:

    def __init__(self, filter_topic: TopicTree, filter):
        self.root = filter_topic
        self.name = self.root.name
        self.filter = filter

        for param in filter.parameters:
            setattr(self, param, self.root.create_child(param))

    def params(self):
        return self.filter.parameters


class Ui:
    """Enum wrapping the UI settings topics tree.
    """

    def __init__(self):
        self.root = TopicTree("ui")

        ui_channels = self.root.create_children([f"ch{ch}" for ch in range(NUM_CHANNELS)])
        iirs = [
            ui_channels[ch].create_children([f"iir{iir}" for iir in range(NUM_IIR_FILTERS_PER_CHANNEL)]) for ch in range(NUM_CHANNELS)
        ]

        for topic in ["filter", "y_offset", "y_min", "y_max", "x_offset"]:
            setattr(self, f"{topic}s", [[
                iirs[ch][iir].create_child(topic)
                for iir in range(NUM_IIR_FILTERS_PER_CHANNEL)
            ] for ch in range(NUM_CHANNELS)])

        for filter in FILTERS:
            setattr(self, f"{filter.filter_type}s", [[
                Filter(iirs[ch][iir].create_child(filter.filter_type), filter)
                for iir in range(NUM_IIR_FILTERS_PER_CHANNEL)
            ] for ch in range(NUM_CHANNELS)])

        # for ch in _ui_channels:
        #     for iir in ch.create_children(["iir0", "iir1"]):
        #         iir.create_children(["filter", "y_offset", "y_min", "y_max", "x_offset"])
        #         for filter in FILTERS:
        #             setattr(self, f"{filter.filter_type}s", [iir.create_child(filter.filter_type) for iir in iirs.children])
        #             filter_topic = iir.create_child(filter.filter_type)
        #             filter_topic.create_children(filter.parameters)
        #     root = _ui_settings

    # Channel topics
    # Access iirs as iirs[ch_idx][iir_idx]
    # channels = _ui_channels
    # iirs = [channel.get_children() for channel in _ui_channels]

    # # Pounder, Clock, and AFE topics.
    # # These are directly readable, and so are not duplicated in the UI settings
    # pounder_channels = _pounder_channels
    # afes = _afe.get_children()
    # clock = _dds_ref_clock


global ui
ui = Ui()

# Root topic. Not using the method `add_child` because it sets the parent of the child
# It's unnecessary to see the sinara root when traversed up the tree when getting the
# path but provides a useful starting point to get all topics when traversing down.
# The MAC address to the Stabilizer board needs to be changed when the app is launched
app_root = TopicTree.new("dt/sinara/fnc/<MAC>")
app_root._children = [stabilizer.root, ui.root]
