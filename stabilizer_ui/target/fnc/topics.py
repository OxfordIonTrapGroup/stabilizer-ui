import logging
from ...topic_tree import TopicTree
from ...iir.filters import FILTERS


logger = logging.getLogger(__name__)

_stabilizer_settings = TopicTree("settings")
_ui_settings = TopicTree("ui")

# Create stabilizer settings topics tree
_stream_target, _afe, _iir, _pounder = _stabilizer_settings.create_children(
    ["stream_target", "afe", "iir_ch", "pounder"])

_afe.create_children(["0", "1"])

# iir_ch/0/1 represents the IIR filter 1 for channel 0
for ch in _iir.create_children(["0", "1"]):
    ch.create_children(["0", "1"])

# Clock settings for stabilizer
_clock, _dds_in, _dds_out = _pounder.create_children(["clock", "in_channel", "out_channel"])
_multiplier, _ext_clk, _ref_clk_freq = _clock.create_children(["multiplier", "external_clock", "reference_clock_frequency"])

# DDS settings for stabilizer.
# in_channel/0 represents the input DDS for channel 0
_dds_in_channels = _dds_in.create_children(["0", "1"])
_dds_out_channels = _dds_out.create_children(["0", "1"])

for dds in _dds_in_channels + _dds_out_channels:
    dds.create_children(
        ["attenuation", "dds/amplitude", "dds/frequency"])

# Create UI settings topics tree
_ui_clk = _ui_settings.create_child("clock")
ui_clk_multiplier, ui_ext_clk, ui_frequency = _ui_clk.create_children(
    ["multiplier", "extClock", "frequency"])

_ui_channels = _ui_settings.create_children(["ch0", "ch1"])
for ch in _ui_channels:
    for iir in ch.create_children(["iir0", "iir1"]):
        iir.create_children(["filter", "y_offset", "y_min", "y_max", "x_offset"])
        for filter in FILTERS:
            filter_topic = iir.create_child(filter.filter_type)
            filter_topic.create_children(filter.parameters)

    ui_afe, ui_pounder = ch.create_children(["afe", "pounder"])
    ch_dds_list = ui_pounder.create_children(["ddsIn", "ddsOut"])
    for dds in ch_dds_list:
        dds.create_children(["attenuation", "amplitude", "frequency"])
    ch_dds_list[0].create_child("track_dds_out")


class Stabilizer:
    """Enum wrapping the stabilizer settings topics tree.
    Topics in an array have separate entries for the parent topic and the subtopics
    """
    root = _stabilizer_settings
    stream_target = _stream_target

    # IIR parent topic group and subtopics
    # Access subtopics as iirs[ch_idx][iir_idx]
    iir = _iir
    iirs = [channel.get_children() for channel in _iir.get_children()]

    # AFE parent topic group and subtopics for each channel
    afe = _afe
    afes = _afe.get_children()

    # Pounder topic
    pounder = _pounder
    # * Clock topics
    clk_multiplier = _multiplier
    clk_freq = _ref_clk_freq
    ext_clk = _ext_clk
    # * DDS parent topic group and subtopics
    dds_in = _dds_in
    dds_ins = _dds_in_channels
    dds_out = _dds_out
    dds_outs = _dds_out_channels

class Ui:
    """Enum wrapping the UI settings topics tree.
    """
    root = _ui_settings

    # Clock topics
    clock = _ui_clk
    clk_multiplier = ui_clk_multiplier
    clk_freq = ui_frequency
    ext_clk = ui_ext_clk

    # Channel topics 
    # Access iirs as iirs[ch_idx][iir_idx]
    channels = _ui_channels
    iirs = [channel.get_children() for channel in _ui_channels]
    afe_channels = [channel.get_child("afe") for channel in _ui_channels]

    # Pounder topics, for both channels
    pounder_channels = [channel.get_child("pounder") for channel in _ui_channels]
    dds_ins = [channel.get_child("pounder/ddsIn") for channel in _ui_channels]
    dds_out = [channel.get_child("pounder/ddsOut") for channel in _ui_channels]

# Root topic. Not using the method `add_child` because it sets the parent of the child
# It's unnecessary to see the sinara root when traversed up the tree when getting the
# path but provides a useful starting point to get all topics when traversing down.
# The MAC address to the Stabilizer board needs to be changed when the app is launched
app_settings_root = TopicTree.new("dt/sinara/fnc/<MAC>")
app_settings_root._children = [_stabilizer_settings, _ui_settings]
