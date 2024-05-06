from typing import Optional, List, Self, Callable

from .mqtt import UiMqttConfig


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
                subtopics[i].set_child(subtopics[i + 1])
            return subtopics[-1]

    def __init__(self, name: str, is_app_root: bool = False):
        """Initialise a new TopicTree node of given name"""
        self._parent = None
        self.name = name
        self.value = None

        self._children = []
        self.mqtt_config = None
        self._is_app_root = is_app_root

    def __str__(self) -> str:
        """String representation for pretty printing"""
        return self.name

    def set_app_root(self, is_app_root: bool = True) -> None:
        """Set whether the node is a breakpoint in the path"""
        self._is_app_root = is_app_root

    def is_app_root(self) -> bool:
        """Check if the node is a breakpoint in the path"""
        return self._is_app_root

    def set_parent(self, parent: Self) -> None:
        """Set the parent of the node and add the node to the parent's children"""
        self._parent = parent
        self._parent.set_children([self])

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
        self.set_child(child.root())
        return child

    def create_children(self, subtopic_names: List[str]) -> List[Self]:
        """Create a list of child nodes with the given names"""
        return [self.get_or_create_child(subtopic) for subtopic in subtopic_names]

    def set_child(self, child_topic: Self) -> None:
        """Add an existing node as a child of the current node"""
        child_topic._parent = self
        self._children.append(child_topic)
        self._value = None

    def set_children(self, children: List[Self]) -> None:
        """Add a list of existing nodes as children of the current node"""
        for child in children:
            self.set_child(child)

    def child(self, path: str) -> Self:
        """Get a child node at the given path. Raises ValueError if the child does not exist."""
        path = path.split("/")
        _child = next((_child for _child in self._children if _child.name == path[0]),
                      None)

        if len(path) == 1:
            return _child
        elif _child is not None:
            return _child.child("/".join(path[1:]))
        else:
            raise ValueError(f"Child {path[0]} not found in topic {self.name}")

    def children(self, paths: Optional[List[str]] = None) -> List[Self]:
        """Get a list of child nodes at the given paths. If no paths are given, returns all children."""
        if paths is None:
            return self._children
        else:
            return [self.child(path) for path in paths]

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
        _child = next((_child for _child in self._children if _child.name == path[0]),
                      None)

        if _child is None:
            return self.create_child("/".join(path))
        elif len(path) == 1:
            return _child
        else:
            return _child.get_or_create_child("/".join(path[1:]))

    def root(self) -> Self:
        """Get the root node of the tree"""
        if self._parent is None:
            return self
        return self._parent.root()

    def app_root(self) -> Self:
        """Get the app_root node of the tree"""
        return self.get_parent_until(lambda x: x._parent.is_app_root())

    def path(self, from_app_root: bool = True, child_path: str = "") -> str:
        r"""Get the string path from the root to the node.
            If `from_app_root` is True, returns the path from *after* the `app_root`
            (or the true root, if there are none).

            e.g. If the topic is true_root/app_root/child1/child2, then the
            path of child2 from app_root is "child1/child2".
            This is to facilitate getting relative paths from the stabilizer topic.

        :param child_path: str: The path from the current node to the child node. Used for recursion.
        :param from_app_root: bool: Whether the path is from the app_root or the true_root.
        """
        if (self._parent is None) or (self._parent.is_app_root() and from_app_root):
            return f"{self.name}{child_path}"
        return self._parent.path(from_app_root, f"/{self.name}{child_path}")

    def get_leaves(self, _leaves=[]) -> list[Self]:
        """Get all leaf nodes of the tree"""
        if not self._children:
            _leaves.append(self)
        else:
            for child in self._children:
                child.get_leaves(_leaves)
        return _leaves

    def update_value(self):
        """Update the value of the node based on the UI MQTT configuration"""
        if self.mqtt_config is not None:
            self.value = self.mqtt_config.read_handler(self.mqtt_config.widgets)
        else:
            raise ValueError("UI MQTT configuration not set for node")
        return self.value

    def bridge_mqtt(self, mqtt_config: UiMqttConfig):
        """Set the UI MQTT configuration for the node"""
        self.mqtt_config = mqtt_config
        self.update_value()
