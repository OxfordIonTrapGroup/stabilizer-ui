import logging
from typing import Optional, List, Self, Callable

from ...iir.filters import FILTERS

logger = logging.getLogger(__name__)


class TopicLevel:

    @classmethod
    def new(cls, name: str, value=None):
        topics = name.split("/")
        if len(topics) == 1:
            return TopicLevel(name)
        else:
            subtopics = [TopicLevel(topic) for topic in topics]
            for i in range(len(subtopics) - 1):
                subtopics[i].add_child(subtopics[i + 1])
            return subtopics[-1]

    def __init__(self, name: str, value=None):
        self._parent = None
        self.name = name

        self._children = []
        self._value = value

    def __repr__(self):
        return self.get_path_from_root()

    def __str__(self) -> str:
        return self.name

    def set_parent(self, parent: Self) -> None:
        self._parent = parent
        self._parent.add_children([self])

    def get_parent(self) -> Optional[Self]:
        return self._parent

    def remove_parent(self) -> None:
        if self._parent is None:
            return
        self._parent._children.remove(self)
        self._parent = None

    def create_child(self, subtopic_name: str) -> Self:
        child = TopicLevel.new(subtopic_name)
        self.add_child(child.root())
        return child

    def create_children(self, subtopic_names: List[str]) -> List[Self]:
        return [self.get_or_create_child(subtopic) for subtopic in subtopic_names]

    def add_child(self, child: Self) -> None:
        child._parent = self
        self._children.append(child)
        self._value = None

    def add_children(self, children: List[Self]) -> None:
        for child in children:
            self.add_child(child)

    def get_children(self) -> List[Self]:
        return self._children

    def remove_children(self) -> None:
        for child in self._children:
            child._parent = None
        self._children = []

    def get_path_from_root(self, child_path: str = "") -> str:
        if self._parent is None:
            return f"{self.name}{child_path}"
        return self._parent.get_path_from_root(f"/{self.name}{child_path}")

    def set_message_value(self, value) -> None:
        if not self._children:
            raise ValueError("This topic has subtopics, it can't have a value")
        self._value = value

    def get_message_value(self):
        return self._value

    def find_child(self, path: str) -> Self:
        path = path.split("/")
        child = next((child for child in self._children if child.name == path[0]), None)

        if len(path) == 1:
            return child
        elif child is not None:
            return child.get_child("/".join(path[1:]))
        else:
            raise ValueError(f"Child {path[0]} not found in topic {self.name}")

    def find_children(self, paths: List[str]) -> List[Self]:
        return [self.get_child(path) for path in paths]

    def get_or_create_child(self, path: str) -> Self:
        path = path.split("/")
        child = next((child for child in self._children if child.name == path[0]), None)

        if child is None:
            return self.create_child("/".join(path))
        elif len(path) == 1:
            return child
        else:
            return child.get_or_create_child("/".join(path[1:]))

    def root(self) -> Self:
        if self._parent is None:
            return self
        return self._parent.root()

    def get_parent_until(self, predicate: Callable[[Self], bool]):
        if predicate(self):
            return self
        if self._parent is None:
            return None
        return self._parent.get_parent_until(predicate)

    def has_children(self) -> bool:
        return bool(self._children)

    def traverse_as_dict(self) -> dict:
        if not self._children:
            return {self.get_path_from_root(): self._value}
        else:
            return {
                key: val
                for child in self._children
                for key, val in child.traverse_as_dict().items()
            }


stabilizer_settings = TopicLevel("settings")
ui_settings = TopicLevel("ui")

# Create stabilizer settings topics tree
_, _afe, _iir = stabilizer_settings.create_children(["stream_target", "afe", "iir_ch"])
_afe.create_children(["0", "1"])
for ch in _iir.create_children(["0", "1"]):
    ch.create_children(["0", "1"])

# Create UI settings topics tree
for ch in ui_settings.create_children(["ch0", "ch1"]):
    for iir in ch.create_children(["iir0", "iir1"]):
        iir.create_children(["filter", "y_offset", "y_min", "y_max", "x_offset"])
        for filter in FILTERS:
            filter_topic = iir.create_child(filter.filter_type)
            filter_topic.create_children(filter.parameters)

# Root topic. Not using the method `add_child` because it sets the parent of the child
# It's unnecessary to see the sinara root when traversed up the tree when getting the
# path but provides a useful starting point to get all topics when traversing down.
# The MAC address to the Stabilizer board needs to be changed when the app is launched
app_settings_root = TopicLevel("dt/sinara/fnc/<MAC>")
app_settings_root._children = [stabilizer_settings, ui_settings]
