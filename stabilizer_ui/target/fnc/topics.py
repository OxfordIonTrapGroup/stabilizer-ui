import logging
from typing import Optional, List, Self, Callable
from enum import Enum, unique

from ...iir.filters import FILTERS
from ...ui_mqtt_bridge import UiMqttConfig

logger = logging.getLogger(__name__)


class TopicTree:

    @classmethod
    def new(cls, name: str):
        """Create a new node at given path. Creates intermediate nodes: does not check if they already exist.
        args:
        * name: str: The path to the topic

        returns:
        * TopicTree: The leaf node of the path
        """
        topics = name.split("/")
        if len(topics) == 1:
            return TopicTree(name)
        else:
            subtopics = [TopicTree(topic) for topic in topics]
            for i in range(len(subtopics) - 1):
                subtopics[i].add_child(subtopics[i + 1])
            return subtopics[-1]

    def __init__(self, name: str):
        """Initialise a new TopicTree node of given name"""
        self._parent = None
        self.name = name
        self.value = None

        self._children = []
        self._ui_mqtt_config = None

    def __repr__(self):
        """Internal string representation, uses path from node"""
        return self.get_path_from_root()

    def __str__(self) -> str:
        """String representation for pretty printing"""
        return self.name

    def set_parent(self, parent: Self) -> None:
        """Set the parent of the node and add the node to the parent's children"""
        self._parent = parent
        self._parent.add_children([self])

    def get_parent(self) -> Optional[Self]:
        """Get the parent of the node. Returns None if the node is a root"""
        return self._parent

    def get_parent_until(self, predicate: Callable[[Self], bool]):
        """Recursively traverse up the tree until the predicate is true.
        args:
        * predicate: Callable(Self) -> bool 
        
        returns: The node where the predicate is true."""
        if predicate(self):
            return self
        if self._parent is None:
            return None
        return self._parent.get_parent_until(predicate)

    def remove_parent(self) -> None:
        """Remove the parent of the node"""
        if self._parent is None:
            return
        self._parent._children.remove(self)
        self._parent = None

    def create_child(self, subtopic_name: str) -> Self:
        """Create a child node with the given name"""
        child = TopicTree.new(subtopic_name)
        self.add_child(child.root())
        return child

    def create_children(self, subtopic_names: List[str]) -> List[Self]:
        """Create a list of child nodes with the given names"""
        return [self.get_or_create_child(subtopic) for subtopic in subtopic_names]

    def add_child(self, child: Self) -> None:
        """Add an existing node as a child of the current node"""
        child._parent = self
        self._children.append(child)
        self._value = None

    def add_children(self, children: List[Self]) -> None:
        """Add a list of existing nodes as children of the current node"""
        for child in children:
            self.add_child(child)

    def get_child(self, path: str) -> Self:
        """Get a child node at the given path. Raises ValueError if the child does not exist."""
        path = path.split("/")
        child = next((child for child in self._children if child.name == path[0]), None)

        if len(path) == 1:
            return child
        elif child is not None:
            return child.get_child("/".join(path[1:]))
        else:
            raise ValueError(f"Child {path[0]} not found in topic {self.name}")

    def get_children(self, paths: Optional[List[str]] = None) -> List[Self]:
        """Get a list of child nodes at the given paths. If no paths are given, returns all children."""
        if paths is None:
            return self._children
        else:
            return [self.get_child(path) for path in paths]

    def has_children(self) -> bool:
        """Check if the node has children"""
        return bool(self._children)

    def remove_children(self) -> None:
        """Remove all children of the node"""
        for child in self._children:
            child._parent = None
        self._children = []

    def get_or_create_child(self, path: str) -> Self:
        """Get a child node at the given path. If the child does not exist, create it and any intermediate nodes."""
        path = path.split("/")
        child = next((child for child in self._children if child.name == path[0]), None)

        if child is None:
            return self.create_child("/".join(path))
        elif len(path) == 1:
            return child
        else:
            return child.get_or_create_child("/".join(path[1:]))

    def root(self) -> Self:
        """Get the root node of the tree"""
        if self._parent is None:
            return self
        return self._parent.root()

    def get_path_from_root(self, child_path: str = "") -> str:
        """Get the string path from the root to the node"""
        if self._parent is None:
            return f"{self.name}{child_path}"
        return self._parent.get_path_from_root(f"/{self.name}{child_path}")

    def get_leaves(self, _leaves=[]) -> list[Self]:
        """Get all leaf nodes of the tree"""
        if not self._children:
            _leaves.append(self)
        else:
            for child in self._children:
                child.get_leaves(_leaves)
        return _leaves

    def bridge_mqtt(self, ui_mqtt_config: UiMqttConfig):
        """Set the UI MQTT configuration for the node"""
        self._ui_mqtt_config = ui_mqtt_config
        self.update_value()

    def update_value(self):
        """Update the value of the node based on the UI MQTT configuration"""
        if self._ui_mqtt_config is not None:
            self.value = self._ui_mqtt_config.read_handler(self._ui_mqtt_config.widgets)
        else: 
            raise ValueError("UI MQTT configuration not set for node")
        return self.value


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
