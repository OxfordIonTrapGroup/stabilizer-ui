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
                subtopics[i].add_child(subtopics[i + 1])
            return subtopics[-1]

    def __init__(self, name: str):
        """Initialise a new TopicTree node of given name"""
        self._parent = None
        self.name = name
        self.value = None

        self.children = []
        self.mqtt_config = None

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
        self._parent.children.remove(self)
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
        self.children.append(child)
        self._value = None

    def add_children(self, children: List[Self]) -> None:
        """Add a list of existing nodes as children of the current node"""
        for child in children:
            self.add_child(child)

    def get_child(self, path: str) -> Self:
        """Get a child node at the given path. Raises ValueError if the child does not exist."""
        path = path.split("/")
        child = next((child for child in self.children if child.name == path[0]), None)

        if len(path) == 1:
            return child
        elif child is not None:
            return child.get_child("/".join(path[1:]))
        else:
            raise ValueError(f"Child {path[0]} not found in topic {self.name}")

    def get_children(self, paths: Optional[List[str]] = None) -> List[Self]:
        """Get a list of child nodes at the given paths. If no paths are given, returns all children."""
        if paths is None:
            return self.children
        else:
            return [self.get_child(path) for path in paths]

    def has_children(self) -> bool:
        """Check if the node has children"""
        return bool(self.children)

    def remove_children(self) -> None:
        """Remove all children of the node"""
        for child in self.children:
            child._parent = None
        self.children = []

    def get_or_create_child(self, path: str) -> Self:
        """Get a child node at the given path. If the child does not exist, create it and any intermediate nodes."""
        path = path.split("/")
        child = next((child for child in self.children if child.name == path[0]), None)

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
        if not self.children:
            _leaves.append(self)
        else:
            for child in self.children:
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
