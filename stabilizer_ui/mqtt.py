from __future__ import annotations

import asyncio
import logging
import json
import math
import numbers
import uuid

from typing import NamedTuple, List, Callable, Any, Dict, Optional
from PyQt6 import QtWidgets
from gmqtt import Client as MqttClient, Subscription

from .firmware import CURRENT, FIRMWARES, Firmware

logger = logging.getLogger(__name__)


def _int_to_bytes(i):
    return i.to_bytes(i.bit_length() // 8 + 1, byteorder="little")


class MiniconfError(Exception):
    """The device reported an error in response to a settings request."""

    def __init__(self, topic: str, message: str):
        super().__init__(f"{topic}: {message}")
        self.topic = topic
        self.message = message


class UnsupportedFirmware(Exception):
    """The device runs a firmware version the UI does not know."""


class MqttInterface:
    """
    Wraps a gmqtt Client to provide a request/response-type interface using the MQTT 5
    response topics/correlation data machinery.

    A timeout is also applied to every request (which is necessary for robustness, as
    Stabilizer only supports QoS 0 for now).

    Settings are given by their key in the current firmware, and `firmware` translates
    them for the device.
    """

    def __init__(self,
                 client: MqttClient,
                 topic_base: str,
                 timeout: float,
                 maxsize: int = 512,
                 fallback_handler: Optional[Callable] = None,
                 on_error: Callable = lambda error: None):
        self._client = client
        self._topic_base = topic_base

        #: Called as `fallback_handler(topic, payload, properties)` for messages other
        #: than responses to our requests (e.g. device status topics).
        self._fallback_handler = fallback_handler
        self._on_error = on_error
        self._error = None

        #: Stores, for each in-flight RPC request, the future waiting for a response,
        #: indexed by the sequence id we used as the MQTT correlation data.
        self._pending = dict[bytes, asyncio.Future]()

        #: Use incrementing sequence id as correlation data to map responses to requests.
        self._next_seq_id = 0

        self._timeout = timeout
        self._maxsize = maxsize

        #: The firmware of the device, see `detect_firmware()`.
        self.firmware: Firmware = CURRENT

        #: Random ID of this client (no real reason to use UUID here over another source
        #: of randomness).
        self.client_id = str(uuid.uuid4()).split("-")[0]
        self._response_base = f"{topic_base}/response_{self.client_id}"
        self._client.on_message = self.handle_message

    def subscribe(self):
        """Start a new session, including after a broker reconnect."""
        self._error = None
        self._client.subscribe(f"{self._response_base}/#")

    def abort(self, error: Exception):
        """Invalidate this session and wake all outstanding requests."""
        self._error = error
        for result in self._pending.values():
            if not result.done():
                result.set_exception(error)

    def check_connection(self):
        if self._error is not None:
            raise self._error

    def topic(self, key: str) -> str:
        """The topic (relative to the topic base) of the setting `key` on the device,
        raising `MiniconfError` if its firmware does not have it."""
        topic = self.firmware.topic(key)
        if topic is None:
            raise MiniconfError(key, f"Not available in firmware {self.firmware}")
        return topic

    async def request(self,
                      key: str,
                      argument: Any,
                      retain: bool = False,
                      timeout: Optional[float] = None) -> str:
        """Set the miniconf leaf `key` (relative to the topic base) to `argument`.

        Returns the response message on success, and raises `MiniconfError` if the
        device reported an error, or `TimeoutError` if it has not responded after
        `timeout` (by default, the timeout of the interface).
        """
        payload = json.dumps(self.firmware.to_device(key, argument)).encode("utf-8")
        return await self._request(self.topic(key), payload, retain, timeout)

    async def get(self, key: str) -> Any:
        """Get the value of the miniconf leaf `key` (relative to the topic base)."""
        # An empty payload requests the value. (It must not be retained, as that would
        # clear the retained value instead.)
        value = json.loads(await self._request(self.topic(key), b"", False))
        return self.firmware.from_device(key, value)

    async def detect_firmware(self) -> Firmware:
        """Find out which firmware the device runs (by getting a setting which only one
        version has at its place), and use it for further requests."""
        for firmware in FIRMWARES:
            self.firmware = firmware
            try:
                await self.get(firmware.probe_key)
            except MiniconfError:
                continue
            logger.info("Stabilizer firmware %s", firmware)
            return firmware
        self.firmware = CURRENT
        raise UnsupportedFirmware("Unknown firmware version: the device has the stream "
                                  "target at none of the places expected")

    def publish(self, key: str, argument: Any, retain: bool = False):
        """Publish `argument` without expecting a response."""
        self.check_connection()
        payload = json.dumps(self.firmware.to_device(key, argument)).encode("utf-8")
        self._client.publish(f"{self._topic_base}/{self.topic(key)}",
                             payload,
                             qos=0,
                             retain=retain)

    async def _request(self,
                       topic: str,
                       payload: bytes,
                       retain: bool,
                       timeout: Optional[float] = None) -> str:
        self.check_connection()
        if timeout is None:
            timeout = self._timeout
        if len(self._pending) > self._maxsize:
            # By construction, `correlation_data` should always be removed from
            # `_pending` either by `handle_message()` or after `_timeout`. If something
            # goes wrong, however, the dictionary could grow indefinitely.
            raise RuntimeError("Too many unhandled requests")
        result = asyncio.Future()
        correlation_data = _int_to_bytes(self._next_seq_id)

        self._pending[correlation_data] = result

        self._next_seq_id += 1
        try:
            self._client.publish(f"{self._topic_base}/{topic}",
                                 payload,
                                 qos=0,
                                 retain=retain,
                                 response_topic=f"{self._response_base}/{topic}",
                                 correlation_data=correlation_data)
            return await asyncio.wait_for(result, timeout)
        except (TimeoutError, OSError) as error:
            if self._error is None:
                self._pending.pop(correlation_data, None)
                result.cancel()
                error = ConnectionError(f"Stabilizer request failed: {topic}: {error!r}")
                self.abort(error)
                self._on_error(error)
            raise error
        finally:
            self._pending.pop(correlation_data, None)

    def handle_message(self, _client, topic, payload, _qos, properties) -> int:
        """Handle a message received by the client (the callback of the client, unless
        several interfaces share it)."""
        if self._error is not None:
            return 0
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
                key = self.firmware.key(request_topic) or request_topic
                result.set_exception(MiniconfError(key, message))
        return 0


def set_will(client: MqttClient, message):
    """Change the will message of `client`, which takes effect when it next connects to
    the broker. (gmqtt only takes it in the constructor, but sends it again with
    `reconnect()`.)"""
    client._will_message = message


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
    elif isinstance(widget, (QtWidgets.QDoubleSpinBox, QtWidgets.QSpinBox)):
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


def get_path(value: dict, path: str) -> Any:
    """The entry of nested dictionaries at `path` (keys separated by `/`)."""
    for name in path.split("/"):
        value = value[name]
    return value


def set_path(value: dict, path: str, entry: Any):
    """Set the entry of nested dictionaries at `path`, creating the missing ones."""
    *parents, name = path.split("/")
    for parent in parents:
        value = value.setdefault(parent, {})
    value[name] = entry


def combine_configs(parts: Dict[str, UiMqttConfig]) -> UiMqttConfig:
    """Bind the widgets of several configs to a single value: nested dictionaries, with
    the value of each part at its key (the path, for nested ones).

    Parts missing from a value which is written are left as they are.
    """

    def read_all(_widgets):
        value = {}
        for path, cfg in parts.items():
            set_path(value, path, cfg.read_handler(cfg.widgets))
        return value

    def write_all(_widgets, value):
        for path, cfg in parts.items():
            try:
                entry = get_path(value, path)
            except (KeyError, TypeError):
                continue
            cfg.write_handler(cfg.widgets, entry)

    widgets = [widget for cfg in parts.values() for widget in cfg.widgets]
    return UiMqttConfig(widgets, read_all, write_all)


def values_match(a, b) -> bool:
    """Whether two setting values are the same, to the precision the device stores
    numbers with (single-precision floating point)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(values_match(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(map(values_match, a, b))

    def is_number(x):
        return isinstance(x, numbers.Real) and not isinstance(x, bool)

    if is_number(a) and is_number(b):
        return math.isclose(a, b, rel_tol=1e-6)
    return a == b


class UiMqttBridge:
    """Keeps the widgets in sync with the MQTT topics they are bound to (`configs`).

    Changes made in the UI are collected in `keys_to_write`, for the owner to write. The
    messages on the topics below the root topic are handled as follows:

    * `ui/...` (UI state, retained on the broker): values published by other clients are
      shown in the widgets. Values retained below one of the keys (e.g.
      `ui/ch0/iir0/pid/Kp` for `ui/ch0/iir0`) are parts of it in an earlier layout, and
      are shown if the key itself has nothing retained.
    * `settings/...` (device settings): only the device knows their values, as it can
      refuse or modify what a client requests. The values the device publishes (which it
      does for all settings after connecting) are passed to `on_device_value`, and the
      requests of other clients to `on_settings_request`, for the owner to read the
      setting back. Both get the key of the setting in the current firmware (see the
      `firmware` of the `MqttInterface`).
    * `alive`, `meta`: the device status.

    Showing a value from the broker in the widgets does not queue it for writing.
    """

    def __init__(self, client: MqttClient, configs: Dict[Any, UiMqttConfig]):
        self.client = client
        self.configs = configs
        self.panicked = False
        self._root_topic = None
        self._ui = None
        self._interface: Optional[MqttInterface] = None
        self._alive_seen = False
        self.connected = asyncio.Event()
        self.on_disconnect: Callable = lambda error: None
        client.on_connect = self._connected
        client.on_disconnect = lambda *_: self.interrupt(
            ConnectionError("Disconnected from MQTT broker"))

        #: Keys of the settings changed in the UI, which still need to be written.
        self.keys_to_write = set()
        #: Set whenever there is something to do for the owner (e.g. keys to write).
        self.updated = asyncio.Event()

        #: Called as `on_device_value(key, value)` with the current value of a setting,
        #: as published by the device.
        self.on_device_value: Callable = lambda key, value: None
        #: Called as `on_settings_request(key)` when another client requests a setting to
        #: be changed. (Whether the device accepted the request is not known.)
        self.on_settings_request: Callable = lambda key: None
        #: Called as `on_ui_value(key)` when displayed UI state changes.
        self.on_ui_value: Callable = lambda key: None
        #: Called as `on_alive(is_alive, retained)` when the device connects to or
        #: disconnects from the broker. `retained` is set if the device was already
        #: connected when we subscribed.
        self.on_alive: Callable = lambda is_alive, retained: None

        #: Set while a value from the broker is written to the widgets.
        self._showing = False
        #: While loading the retained UI state, the keys it has a value for, and the
        #: parts of values in an earlier layout (by key, then path below it).
        self._loading = False
        self._ui_retained = set()
        self._ui_legacy = dict[str, dict[str, Any]]()

    @classmethod
    async def new(cls, broker_address: NetworkAddress, configs, **kwargs):
        client = MqttClient(client_id="", **kwargs)
        bridge = cls(client, configs)
        await client.connect(broker_address.get_ip(),
                             port=broker_address.port,
                             keepalive=10)
        return bridge

    def _connected(self, *_):
        self.connected.set()
        self.updated.set()

    def interrupt(self, error: Exception):
        """Drop unconfirmed work; the owner resynchronises after MQTT reconnects."""
        self.connected.clear()
        self.keys_to_write.clear()
        if self._interface is not None:
            self._interface.abort(error)
        self.on_disconnect(error)
        self.updated.set()

    def handle_message(self, topic: str, payload: bytes, properties: dict):
        """Handle a message below the root topic (other than the responses to our
        requests): see the class documentation."""
        if self._root_topic is None or not topic.startswith(self._root_topic + "/"):
            logger.debug("Ignoring unrelated topic: %s", topic)
            return
        key = topic[len(self._root_topic) + 1:]
        retained = bool(properties.get("retain"))

        if key == "alive":
            self._handle_alive(payload, retained)
        elif key == "meta":
            self._handle_meta(payload)
        elif not payload:
            # A request for the value of a setting, or a retained message being cleared.
            pass
        elif key.startswith("settings/"):
            self._handle_settings_message(key, payload, properties, retained)
        elif key.startswith("ui/"):
            self._handle_ui_message(key, payload, properties, retained)

    def _handle_alive(self, payload: bytes, retained: bool):
        # The device publishes a retained `1` while connected, and its will clears the
        # retained message (empty payload) when it disconnects.
        self._alive_seen = True
        try:
            is_alive = bool(payload) and bool(json.loads(payload))
        except ValueError:
            is_alive = True
        logger.info(f"Stabilizer {'alive' if is_alive else 'offline'}")
        self.on_alive(is_alive, retained)

    def _handle_meta(self, payload: bytes):
        # Published (not retained) once each time the device connects to the broker.
        try:
            meta = json.loads(payload)
        except ValueError:
            logger.warning("Failed to parse device metadata: %s", payload)
            return
        logger.info("Stabilizer firmware %s (%s, hardware %s)",
                    meta.get("firmware_version"), meta.get("profile"),
                    meta.get("hardware_version"))
        panic_info = meta.get("panic_info", "None")
        has_panicked = panic_info != "None"
        self.panicked = has_panicked
        if has_panicked:
            logger.error("Stabilizer had panicked, but has restarted: %s", panic_info)
        self._ui.update_panic_status(has_panicked, panic_info)

    def _handle_settings_message(self, topic: str, payload: bytes, properties: dict,
                                 retained: bool):
        if retained:
            # A request from before we subscribed. The device might not have accepted
            # it, or have been set to something else since (not retained).
            return
        firmware = self._interface.firmware
        key = firmware.key(topic)
        if key is None:
            logger.debug("Ignoring message topic '%s'", topic)
            return
        code = dict(properties.get("user_property", [])).get("code")
        if code is None:
            # (Firmware v0.9 publishes its settings after connecting like this as well.)
            self.on_settings_request(key)
        elif code == "Ok":
            # The device publishes the values of its settings on their topics (with the
            # response code): all of them after it has connected, and those a client
            # asks for without giving a response topic.
            try:
                value = firmware.from_device(key, json.loads(payload))
            except (ValueError, KeyError, TypeError):
                logger.warning("Failed to parse the value of '%s': %s", topic, payload)
                return
            self.on_device_value(key, value)

    def _handle_ui_message(self, key: str, payload: bytes, properties: dict,
                           retained: bool):
        if key not in self.configs:
            if self._loading and retained:
                self._legacy_part_received(key, payload)
            else:
                logger.debug("Ignoring message topic '%s'", key)
            return
        if self._loading:
            self._ui_retained.add(key)
        if key in self.keys_to_write:
            # The local edit has not been sent yet. Preserve its latest widget value.
            return
        try:
            value = json.loads(payload)
        except ValueError:
            logger.warning("Failed to parse the value of '%s': %s", key, payload)
            return
        if self.show(key, value):
            self.on_ui_value(key)

    def _legacy_part_received(self, topic: str, payload: bytes):
        """Keep a retained part of the value of a key in an earlier layout."""
        parts = topic.split("/")
        for i in range(len(parts) - 1, 1, -1):
            key = "/".join(parts[:i])
            if key in self.configs:
                break
        else:
            logger.debug("Ignoring message topic '%s'", topic)
            return
        try:
            value = json.loads(payload)
        except ValueError:
            logger.warning("Failed to parse the value of '%s': %s", topic, payload)
            return
        self._ui_legacy.setdefault(key, {})["/".join(parts[i:])] = value

    def _show_legacy_values(self):
        """Show the retained parts of the keys which have no value of their own."""
        for key, parts in self._ui_legacy.items():
            if key in self._ui_retained:
                continue
            logger.info("Using the UI state of '%s' in the earlier layout", key)
            cfg = self.configs[key]
            value = cfg.read_handler(cfg.widgets)
            for path, part in parts.items():
                set_path(value, path, part)
            if self.show(key, value):
                self.on_ui_value(key)
        self._ui_legacy.clear()
        self._ui_retained.clear()

    def show(self, key: str, value: Any) -> bool:
        """Show a value from the broker or the device in the widgets bound to `key`,
        without queueing it for writing. Returns whether this changed the widgets."""
        cfg = self.configs[key]
        try:
            if values_match(cfg.read_handler(cfg.widgets), value):
                # Leave the widgets alone (the user might be editing them).
                return False
            logger.info("Showing '%s' = %s", key, value)
            self._showing = True
            try:
                for widget in cfg.widgets:
                    if widget is not None:
                        widget.setProperty("mqtt_showing", True)
                cfg.write_handler(cfg.widgets, value)
            finally:
                for widget in cfg.widgets:
                    if widget is not None:
                        widget.setProperty("mqtt_showing", False)
                self._showing = False
        except Exception:
            logger.warning("Failed to show '%s' = %s", key, value, exc_info=True)
            return False
        return True

    def queue_write(self, key: str):
        """Coalesce edits; the worker reads the latest widgets when sending the key."""
        self.keys_to_write.add(key)
        self.updated.set()

    async def load_ui(self, root_topic: str, ui: AbstractUiWindow,
                      interface: MqttInterface):
        """Subscribe to the topics below `root_topic` (for the whole session), and show
        the UI state retained on the broker.

        `interface` is used to publish, and needs to have `handle_message()` as its
        `fallback_handler`.
        """
        self._root_topic = root_topic
        self._ui = ui
        self._interface = interface
        self._alive_seen = False
        self._ui_retained.clear()
        self._ui_legacy.clear()
        interface.subscribe()

        logger.info(f"Subscribing to the settings at {root_topic}")
        self._loading = True
        self.client.subscribe([
            # There is no point in receiving our own requests.
            Subscription(f"{root_topic}/settings/#", no_local=True),
            # Include our echoes so every client follows the broker's ordering.
            Subscription(f"{root_topic}/ui/#"),
            Subscription(f"{root_topic}/alive"),
            Subscription(f"{root_topic}/meta"),
        ])
        # Based on testing, all the retained messages are sent immediately after
        # subscribing, but add some delay in case this is actually a race condition.
        await asyncio.sleep(1)
        interface.check_connection()
        self._loading = False
        self._show_legacy_values()

        if not self._alive_seen:
            # `alive` is only retained while the device is connected.
            logger.warning("Stabilizer offline (no retained alive message)")
            ui.update_alive_status(False)

    def connect_ui(self):
        """Set up UI signals"""

        # Capture loop variable.
        def make_queue(key):

            def queue(*args):
                if not self._showing:
                    self.queue_write(key)

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
