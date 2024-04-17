import asyncio
from typing import NamedTuple, List, Callable, Any, Dict
import logging
import json

from PyQt5 import QtWidgets
from gmqtt import Client as MqttClient
from .widgets.ui import AbstractUiWindow

logger = logging.getLogger(__name__)


class NetworkAddress(NamedTuple):
    ip: List[int]
    port: int = 9293

    def get_ip(self) -> str:
        return ".".join(map(str, self.ip))


def read(widgets):
    assert len(widgets) == 1, "Default read() only implemented for one widget"
    widget = widgets[0]

    if isinstance(
            widget,
        (
            QtWidgets.QCheckBox,
            QtWidgets.QRadioButton,
            QtWidgets.QGroupBox,
        ),
    ):
        return widget.isChecked()

    if isinstance(widget, (QtWidgets.QDoubleSpinBox, QtWidgets.QSpinBox)):
        return widget.value()

    if isinstance(widget, QtWidgets.QComboBox):
        return widget.currentText()

    assert f"Widget type not handled: {widget}"


def write(widgets, value):
    assert len(widgets) == 1, "Default write() only implemented for one widget"
    widget = widgets[0]

    if isinstance(
            widget,
        (
            QtWidgets.QCheckBox,
            QtWidgets.QRadioButton,
            QtWidgets.QGroupBox,
        ),
    ):
        widget.setChecked(value)
    elif isinstance(widget, QtWidgets.QDoubleSpinBox):
        widget.setValue(value)
    elif isinstance(widget, QtWidgets.QComboBox):
        options = [widget.itemText(i) for i in range(widget.count())]
        widget.setCurrentIndex(options.index(value))
    else:
        assert f"Widget type not handled: {widget}"


class UiMqttConfig(NamedTuple):
    widgets: List[QtWidgets.QWidget]
    read_handler: Callable = read
    write_handler: Callable = write


class UiMqttBridge:

    def __init__(self, client: MqttClient, configs: Dict[Any, UiMqttConfig]):
        self.client = client
        self.configs = configs
        self.panicked = False

    @classmethod
    async def new(cls, broker_address: NetworkAddress, *args, **kwargs):
        client = MqttClient(client_id="")
        host, port = broker_address.get_ip(), broker_address.port
        try:
            await client.connect(host, port=port, keepalive=10)
            logger.info(f"Connected to MQTT broker at {host}:{port}.")
        except Exception as connect_exception:
            logger.error("Failed to connect to MQTT broker: %s", connect_exception)
            raise connect_exception

        return cls(client, *args, **kwargs)

    async def load_ui(self, objectify: Callable, root_topic: str, ui: AbstractUiWindow):
        """Load current settings from MQTT"""
        retained_settings = {}

        def panic_handler(value):
            has_panicked = (json.loads(value) is not None)
            ui.onPanicStatusChange(has_panicked, value)
            if has_panicked:
                logger.error("Stabilizer had panicked, but has restarted")

        def alive_handler(value, is_initial_subscription=False):
            is_alive = bool(json.loads(value))
            ui.onlineStatusChange(is_alive)
            logger.info(f"Stabilizer {'alive' if is_alive else 'offline'}")

        def collect_settings(_client, topic, value, _qos, _properties):
            subtopic = topic[len(root_topic) + 1:]
            try:
                key = objectify(subtopic)
                decoded_value = json.loads(value)
                retained_settings[key] = decoded_value
                logger.info(
                    "Registering message topic '#/%s' with value '%s'",
                    subtopic,
                    decoded_value,
                )
                if subtopic == "meta/panic":
                    panic_handler(value)
                elif subtopic == "alive":
                    alive_handler(value)
            except ValueError:
                logger.info("Ignoring message topic '%s'", subtopic)
            return 0

        self.client.on_message = collect_settings

        logger.info(f"Subscribing to all settings at {root_topic}/#")
        all_settings = f"{root_topic}/#"
        self.client.subscribe(all_settings)
        # Based on testing, all the retained messages are sent immediately after
        # subscribing, but add some delay in case this is actually a race condition.
        await asyncio.sleep(1)
        self.client.unsubscribe(all_settings)

        self.client.subscribe(f"{root_topic}/meta/panic")
        self.client.subscribe(f"{root_topic}/alive")

        for retained_key, retained_value in retained_settings.items():
            if retained_key in self.configs:
                cfg = self.configs[retained_key]
                cfg.write_handler(cfg.widgets, retained_value)

    def connect_ui(self):
        """Set up UI signals"""
        keys_to_write = set()
        ui_updated = asyncio.Event()

        # Capture loop variable.
        def make_queue(key):

            def queue(*args):
                keys_to_write.add(key)
                ui_updated.set()

            return queue

        for key, cfg in self.configs.items():
            queue = make_queue(key)
            for widget in cfg.widgets:
                if not widget:
                    continue
                elif hasattr(widget, "valueChanged"):
                    widget.valueChanged.connect(queue)
                elif hasattr(widget, "toggled"):
                    widget.toggled.connect(queue)
                elif hasattr(widget, "activated"):
                    widget.activated.connect(queue)
                else:
                    assert f"Widget type not handled: {widget}"

            keys_to_write.add(key)  # write once at startup
        return keys_to_write, ui_updated

