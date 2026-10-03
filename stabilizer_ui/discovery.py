"""Finding the Stabilizers on MQTT brokers, and the firmware they run."""
from __future__ import annotations

import asyncio
import json
import logging

from dataclasses import dataclass
from typing import Callable, Optional
from gmqtt import Client as MqttClient

from .firmware import Firmware, FirmwareV09
from .mqtt import MiniconfError, MqttInterface, NetworkAddress, UnsupportedFirmware

logger = logging.getLogger(__name__)

#: The UI targets (modules in `stabilizer_ui.target`), by the name of the application in
#: the MQTT prefix (the name of the firmware binary).
TARGETS = {"dual-iir": "dual_iir", "fnc": "fnc", "current_sense": "current_sense"}

#: Time to wait for the retained `alive` messages after subscribing, in seconds.
RETAINED_TIMEOUT = 1.0

#: Time to wait for the device to answer when asking for its firmware, in seconds.
PROBE_TIMEOUT = 2.0


@dataclass
class Device:
    broker: NetworkAddress
    #: The application, as in the MQTT prefix (e.g. `dual-iir`).
    app: str
    #: The MQTT ID (the MAC address, unless configured otherwise).
    id: str
    #: Whether the device is connected to the broker (`None` if unknown).
    alive: Optional[bool] = None
    firmware: Optional[Firmware] = None
    #: Why the firmware is not known, if it is not.
    error: Optional[str] = None

    @property
    def prefix(self) -> str:
        return f"dt/sinara/{self.app}/{self.id}"

    @property
    def target(self) -> Optional[str]:
        """The module of the UI target, or `None` if the application is not supported."""
        return TARGETS.get(self.app)

    def __str__(self):
        return f"{self.app}/{self.id}"


def parse_broker(address: str) -> NetworkAddress:
    """`host[:port]` (the host as IPv4 address), with port 1883 by default."""
    host, _, port = address.partition(":")
    return NetworkAddress.from_str_ip(host, int(port or 1883))


async def discover(brokers: list[NetworkAddress],
                   match: Optional[Callable[[Device], bool]] = None) -> list[Device]:
    """The devices on `brokers` with a retained `alive` message (and for which `match`
    is true, if given), with their firmware.

    Connected devices have one, as do those of firmware v0.9 which have disconnected
    (later ones clear it). A broker which cannot be reached is skipped (and logged).
    """
    results = await asyncio.gather(*(_discover(broker, match) for broker in brokers),
                                   return_exceptions=True)
    devices = []
    for broker, result in zip(brokers, results):
        if isinstance(result, Exception):
            logger.warning("Failed to search %s: %r", broker.get_ip(), result)
        else:
            devices += result
    return devices


async def _discover(broker: NetworkAddress,
                    match: Optional[Callable[[Device], bool]]) -> list[Device]:
    devices = dict[str, Device]()
    interfaces = dict[str, MqttInterface]()

    def handle_message(client, topic, payload, qos, properties):
        parts = topic.split("/")
        if len(parts) == 5 and parts[4] == "alive" and properties.get("retain"):
            device = Device(broker, parts[2], parts[3])
            try:
                device.alive = bool(payload) and bool(json.loads(payload))
            except ValueError:
                device.alive = True
            if payload == b"0":
                # Only v0.9 publishes this (when disconnecting).
                device.firmware = FirmwareV09()
            if match is None or match(device):
                devices[device.prefix] = device
            return 0
        for prefix, interface in interfaces.items():
            if topic.startswith(prefix + "/"):
                return interface.handle_message(client, topic, payload, qos, properties)
        return 0

    client = MqttClient(client_id="")
    client.on_message = handle_message
    await client.connect(broker.get_ip(), port=broker.port, keepalive=10)
    try:
        client.subscribe("dt/sinara/+/+/alive")
        await asyncio.sleep(RETAINED_TIMEOUT)
        probed = [
            device for device in devices.values()
            if device.alive and device.target is not None
        ]
        for device in probed:
            interface = MqttInterface(client, device.prefix, timeout=PROBE_TIMEOUT)
            client.on_message = handle_message
            interface.subscribe()
            interfaces[device.prefix] = interface
        await asyncio.gather(*(_probe(device, interfaces[device.prefix])
                               for device in probed))
    finally:
        await client.disconnect()
    return sorted(devices.values(), key=lambda device: (device.app, device.id))


async def _probe(device: Device, interface: MqttInterface):
    try:
        device.firmware = await interface.detect_firmware()
    except (UnsupportedFirmware, MiniconfError) as e:
        device.error = str(e)
    except (ConnectionError, TimeoutError):
        device.error = "No answer"
