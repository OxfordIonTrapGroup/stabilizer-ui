import asyncio
import logging
import json
import uuid

from typing import NamedTuple, List, Callable, Any, Dict, Optional
from contextlib import suppress
from PyQt5 import QtWidgets
from gmqtt import Client as MqttClient, Message as MqttMessage
from .widgets import AbstractUiWindow

logger = logging.getLogger(__name__)


def _int_to_bytes(i):
    return i.to_bytes(i.bit_length() // 8 + 1, byteorder="little")


class MqttInterface:
    """
    Wraps a gmqtt Client to provide a request/response-type interface using the MQTT 5
    response topics/correlation data machinery.

    A timeout is also applied to every request (which is necessary for robustness, as
    Stabilizer only supports QoS 0 for now).
    """

    def __init__(self,
                 client: MqttClient,
                 topic_base: str,
                 timeout: float,
                 maxsize: int = 512):
        self._client = client
        self._topic_base = topic_base
        self._pending = {}
        self._timeout = timeout
        self._maxsize = maxsize

        #: Use incrementing sequence id as correlation data to map responses to requests
        #: (together with client id).
        self._next_seq_id = 0

        # Generate a random client ID (no real reason to use UUID here over another
        # source of randomness).
        client_id = str(uuid.uuid4()).split("-")[0]
        self._response_base = f"{topic_base}/response_{client_id}"
        self._client.subscribe(f"{self._response_base}/#")

        self._client.on_message = self._on_message

    async def request(self, topic: str, argument: Any, retain: bool = False):
        if len(self._pending) > self._maxsize:
            # By construction, `correlation_data` should always be removed from
            # `_pending` either by `_on_message()` or after `_timeout`. If something
            # goes wrong, however the dictionary could grow indefinitely.
            raise RuntimeError("Too many unhandled requests")
        result = asyncio.Future()
        correlation_data = _int_to_bytes(self._next_seq_id)

        self._pending[correlation_data] = result

        payload = json.dumps(argument).encode("utf-8")
        self._client.publish(
            f"{self._topic_base}/{topic}",
            payload,
            qos=0,
            retain=retain,
            response_topic=f"{self._response_base}/{topic}",
            correlation_data=correlation_data,
        )
        self._next_seq_id += 1

        async def fail_after_timeout():
            await asyncio.sleep(self._timeout)
            result.set_exception(
                TimeoutError(f"No response to {topic} request after {self._timeout} s"))
            self._pending.pop(correlation_data)

        _, pending = await asyncio.wait(
            [result, asyncio.create_task(fail_after_timeout())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
            with suppress(asyncio.CancelledError):
                await p
        return await result

    def _on_message(self, _client, topic, payload, _qos, properties) -> int:
        if not topic.startswith(self._response_base):
            logger.debug("Ignoring unrelated topic: %s", topic)
            return 0

        cd = properties.get("correlation_data", [])
        if len(cd) != 1:
            logger.warning(
                ("Received response without (valid) correlation data"
                 "(topic '%s', payload %s) "),
                topic,
                payload,
            )
            return 0
        seq_id = cd[0]

        if seq_id not in self._pending:
            # This is fine if Stabilizer restarts, though.
            logger.warning("Received unexpected/late response for '%s' (id %s)", topic,
                           seq_id)
            return 0

        result = self._pending.pop(seq_id)
        if not result.cancelled():
            try:
                # Would like to json.loads() here, but the miniconf responses are
                # unfortunately plain strings still (see quartiq/miniconf#32).
                result.set_result(payload)
            except BaseException as e:
                err = ValueError(f"Failed to parse response for '{topic}'")
                err.__cause__ = e
                result.set_exception(err)
        return 0
    

class NetworkAddress(NamedTuple):
    ip: List[int]
    port: int = 9293

    @classmethod
    def from_str_ip(cls, ip: str, port: int):
        _ip = list(map(int, ip.split(".")))
        return cls(_ip, port)

    def get_ip(self) -> str:
        return ".".join(map(str, self.ip))

    def is_unspecified(self):
        """Mirrors `smoltcp::wire::IpAddress::is_unspecified` in Rust, for IPv4 addresses"""
        return self.ip == [0, 0, 0, 0]


NetworkAddress.UNSPECIFIED = NetworkAddress([0, 0, 0, 0], 0)


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
        r"""Factory method to create a new MQTT connection
            :param broker_address: Address of the MQTT broker
            :type broker_address: NetworkAddress
            :param args: Additional arguments to pass to the constructor

            :Keyword Arguments:
                * *will_message* (``gmqtt.Message``) -- Last will and testament message
                * *kwargs* -- Additional keyword arguments to pass to the constructor

            :return: A new instance of UiMqttBridge

        """
        will_message: Optional[MqttMessage] = kwargs.pop("will_message", None)
        client = MqttClient(client_id="", will_message=will_message)
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
