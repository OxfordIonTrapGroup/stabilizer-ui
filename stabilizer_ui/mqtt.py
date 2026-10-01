from __future__ import annotations

import asyncio
import logging
import json
import uuid

from typing import NamedTuple, List, Callable, Any, Dict, Optional
from contextlib import suppress
from PyQt6 import QtWidgets
from gmqtt import Client as MqttClient, Message as MqttMessage

logger = logging.getLogger(__name__)


def _int_to_bytes(i):
    return i.to_bytes(i.bit_length() // 8 + 1, byteorder="little")


class MiniconfError(Exception):
    """The device reported an error in response to a settings request."""

    def __init__(self, topic: str, message: str):
        super().__init__(f"{topic}: {message}")
        self.topic = topic
        self.message = message


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
                 maxsize: int = 512,
                 fallback_handler: Optional[Callable] = None):
        self._client = client
        self._topic_base = topic_base

        #: Called as `fallback_handler(topic, payload, properties)` for messages other
        #: than responses to our requests (e.g. device status topics).
        self._fallback_handler = fallback_handler

        #: Stores, for each in-flight RPC request, the future waiting for a response,
        #: indexed by the sequence id we used as the MQTT correlation data.
        self._pending = dict[bytes, asyncio.Future]()

        #: Use incrementing sequence id as correlation data to map responses to requests.
        self._next_seq_id = 0

        self._timeout = timeout
        self._maxsize = maxsize

        # Generate a random client ID (no real reason to use UUID here over another
        # source of randomness).
        client_id = str(uuid.uuid4()).split("-")[0]
        self._response_base = f"{topic_base}/response_{client_id}"
        self._client.subscribe(f"{self._response_base}/#")

        self._client.on_message = self._on_message

    async def request(self, topic: str, argument: Any, retain: bool = False) -> str:
        """Set the miniconf leaf at `topic` (relative to the topic base) to `argument`.

        Returns the response message on success, and raises `MiniconfError` if the
        device reported an error.
        """
        if len(self._pending) > self._maxsize:
            # By construction, `correlation_data` should always be removed from
            # `_pending` either by `_on_message()` or after `_timeout`. If something
            # goes wrong, however, the dictionary could grow indefinitely.
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
            if self._fallback_handler is not None:
                self._fallback_handler(topic, payload, properties)
            else:
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

        # Success or failure is signalled through the `code` user property; the
        # payload is just a message (`OK` or a description of the error).
        code = dict(properties.get("user_property", [])).get("code")
        if code == "Continue":
            # Only sent for multi-part (list/dump) responses, which we do not request.
            logger.debug("Ignoring multi-part response for '%s'", topic)
            return 0

        if seq_id not in self._pending:
            # This is fine if Stabilizer restarts, though.
            logger.warning("Received unexpected/late response for '%s' (id %s)", topic,
                           seq_id)
            return 0

        result = self._pending.pop(seq_id)
        if not result.done():
            message = payload.decode("utf-8", errors="replace")
            if code == "Ok":
                result.set_result(message)
            else:
                request_topic = topic[len(self._response_base) + 1:]
                result.set_exception(MiniconfError(request_topic, message))
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

    def __str__(self) -> str:
        """Format as `a.b.c.d:port`, as used for the stream target setting."""
        return f"{self.get_ip()}:{self.port}"

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
        self._root_topic = None
        self._ui = None
        self._alive_seen = False

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

    def handle_status_message(self, topic: str, payload: bytes, _properties=None) -> bool:
        """Handle the device status topics (`alive`, `meta`) below the root topic.

        Returns whether `topic` was a status topic.
        """
        if self._root_topic is None or not topic.startswith(self._root_topic + "/"):
            return False
        subtopic = topic[len(self._root_topic) + 1:]

        if subtopic == "alive":
            # The device publishes a retained `1` while connected, and its will clears
            # the retained message (empty payload) when it disconnects.
            self._alive_seen = True
            try:
                is_alive = bool(payload) and bool(json.loads(payload))
            except ValueError:
                is_alive = True
            self._ui.update_alive_status(is_alive)
            logger.info(f"Stabilizer {'alive' if is_alive else 'offline'}")
            return True

        if subtopic == "meta":
            # Published (not retained) once each time the device connects to the broker.
            try:
                meta = json.loads(payload)
            except ValueError:
                logger.warning("Failed to parse device metadata: %s", payload)
                return True
            logger.info("Stabilizer firmware %s (%s, hardware %s)",
                        meta.get("firmware_version"), meta.get("profile"),
                        meta.get("hardware_version"))
            panic_info = meta.get("panic_info", "None")
            has_panicked = panic_info != "None"
            self.panicked = has_panicked
            if has_panicked:
                logger.error("Stabilizer had panicked, but has restarted: %s", panic_info)
            self._ui.update_panic_status(has_panicked, panic_info)
            return True

        return False

    async def load_ui(self, objectify: Callable, root_topic: str, ui: AbstractUiWindow):
        """Load current settings from MQTT"""
        retained_settings = {}
        self._root_topic = root_topic
        self._ui = ui

        def collect_settings(_client, topic, value, _qos, _properties):
            if self.handle_status_message(topic, value):
                return 0
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

        self.client.subscribe(f"{root_topic}/meta")
        self.client.subscribe(f"{root_topic}/alive")

        if not self._alive_seen:
            # `alive` is only retained while the device is connected.
            logger.warning("Stabilizer offline (no retained alive message)")
            ui.update_alive_status(False)

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
