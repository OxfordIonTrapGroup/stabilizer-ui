"""Finding the Stabilizers on MQTT brokers, their names, and the firmware they run."""
from __future__ import annotations

import asyncio
import json
import logging

from dataclasses import dataclass
from typing import Callable, Optional
from gmqtt import Client as MqttClient, Subscription

from .firmware import Firmware, Metadata, by_name, describe, describe_details
from .mqtt import (DEVICE_NAME_KEY, MiniconfError, MqttInterface, NetworkAddress,
                   UnsupportedFirmware)

logger = logging.getLogger(__name__)

#: The UI targets (modules in `stabilizer_ui.target`), by the name of the application in
#: the MQTT prefix (the name of the firmware binary).
TARGETS = {
    "dual-iir": "dual_iir",
    "fnc": "fnc",
    "current_sense": "current_sense",
    "l674": "l674",
}

#: Time to wait for the retained messages after subscribing, in seconds.
RETAINED_TIMEOUT = 1.0

#: Time to wait for the device to answer when asking for its firmware (if its build
#: metadata does not tell), in seconds.
PROBE_TIMEOUT = 2.0

#: Time to wait for the broker to take a new name, in seconds.
RENAME_TIMEOUT = 5.0


@dataclass
class Device:
    broker: NetworkAddress
    #: The application, as in the MQTT prefix (e.g. `dual-iir`).
    app: str
    #: The MQTT ID (the MAC address, unless configured otherwise).
    id: str
    #: Whether the device is connected to the broker (`None` if unknown).
    alive: Optional[bool] = None
    #: The name the user has given the device (empty if none).
    name: str = ""
    #: The firmware it runs (or, if it is not connected, ran last), if known.
    firmware: Optional[Firmware] = None
    #: The build metadata of the firmware, if retained on the broker (by firmware from
    #: October 2026).
    metadata: Optional[Metadata] = None
    #: Why the firmware is not known, if it is not.
    error: Optional[str] = None

    @property
    def prefix(self) -> str:
        return f"dt/sinara/{self.app}/{self.id}"

    @property
    def label(self) -> str:
        """The name, or the ID if there is none."""
        return self.name or self.id

    @property
    def target(self) -> Optional[str]:
        """The module of the UI target, or `None` if the application is not supported."""
        return TARGETS.get(self.app)

    @property
    def firmware_label(self) -> str:
        """The firmware to show (see `firmware.describe()`), or why it is not known."""
        if self.firmware is None and (self.metadata is None or not self.metadata.version):
            if self.target is None:
                return "–"
            return f"unknown ({self.error})" if self.error else "unknown"
        return describe(self.firmware, self.metadata)

    @property
    def firmware_details(self) -> str:
        """An explanation of `firmware_label` (see `firmware.describe_details()`)."""
        return describe_details(self.firmware, self.metadata)

    def __str__(self):
        return f"{self.app}/{self.id}"


def parse_broker(address: str) -> NetworkAddress:
    """`host[:port]` (the host as IPv4 address), with port 1883 by default."""
    host, _, port = address.partition(":")
    return NetworkAddress.from_str_ip(host, int(port or 1883))


async def discover(brokers: list[NetworkAddress],
                   match: Optional[Callable[[Device], bool]] = None) -> list[Device]:
    """The devices on `brokers` with a retained `alive` message or name (and for which
    `match` is true, if given), with their firmware.

    Connected devices have an `alive` message, as do those of firmware v0.9 which have
    disconnected (later ones clear it). Devices which only have a name are taken as
    disconnected. A broker which cannot be reached is skipped (and logged).

    The firmware of a device is told by its retained build metadata (`meta`, by
    firmware from October 2026) if possible; otherwise, connected devices are asked (see
    `MqttInterface.detect_firmware()`).
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
    #: The retained `alive` messages, names and build metadata, by device prefix.
    alive = dict[str, bytes]()
    names = dict[str, str]()
    metadata = dict[str, Metadata]()
    interfaces = dict[str, MqttInterface]()

    def handle_message(client, topic, payload, qos, properties):
        parts = topic.split("/")
        prefix, key = "/".join(parts[:4]), "/".join(parts[4:])
        if properties.get("retain") and key in ("alive", "meta", DEVICE_NAME_KEY):
            if key == "alive":
                alive[prefix] = payload
            elif key == "meta":
                try:
                    metadata[prefix] = Metadata.parse(payload)
                except ValueError:
                    logger.warning("Failed to parse the metadata of %s: %s", prefix,
                                   payload)
            else:
                try:
                    name = json.loads(payload)
                except ValueError:
                    name = None
                if isinstance(name, str) and name.strip():
                    names[prefix] = name.strip()
            return 0
        if prefix in interfaces:
            return interfaces[prefix].handle_message(client, topic, payload, qos,
                                                     properties)
        return 0

    client = MqttClient(client_id="")
    client.on_message = handle_message
    await client.connect(broker.get_ip(), port=broker.port, keepalive=10)
    try:
        client.subscribe([
            Subscription("dt/sinara/+/+/alive"),
            Subscription("dt/sinara/+/+/meta"),
            Subscription(f"dt/sinara/+/+/{DEVICE_NAME_KEY}")
        ])
        await asyncio.sleep(RETAINED_TIMEOUT)
        devices = []
        for prefix in alive.keys() | names.keys():
            _, _, app, device_id = prefix.split("/")
            device = Device(broker,
                            app,
                            device_id,
                            name=names.get(prefix, ""),
                            metadata=metadata.get(prefix))
            payload = alive.get(prefix, b"")
            try:
                device.alive = bool(payload) and bool(json.loads(payload))
            except ValueError:
                device.alive = True
            if payload == b"0":
                # Only v0.9 publishes this (when disconnecting).
                device.firmware = by_name("v0.9")
            elif device.metadata is not None:
                device.firmware = device.metadata.firmware
            if match is None or match(device):
                devices.append(device)
        probed = [
            device for device in devices
            if device.alive and device.target is not None and device.firmware is None
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
    # Those with a name first.
    return sorted(devices, key=lambda device: (not device.name, device.label.lower()))


async def rename(device: Device, name: str):
    """Give `device` a new name (empty for none), retained on its broker, where every UI
    of the device follows it. Returns once the broker has passed it on."""
    topic = f"{device.prefix}/{DEVICE_NAME_KEY}"
    payload = json.dumps(name).encode("utf-8")
    passed_on = asyncio.get_running_loop().create_future()

    def handle_message(client, msg_topic, msg_payload, qos, properties):
        # Skip the name retained before, which arrives first on subscribing.
        if (msg_topic == topic and msg_payload == payload and not properties.get("retain")
                and not passed_on.done()):
            passed_on.set_result(None)
        return 0

    client = MqttClient(client_id="")
    client.on_message = handle_message
    async with asyncio.timeout(RENAME_TIMEOUT):
        await client.connect(device.broker.get_ip(),
                             port=device.broker.port,
                             keepalive=10)
        try:
            client.subscribe(Subscription(topic))
            client.publish(topic, payload, qos=0, retain=True)
            await passed_on
        finally:
            await client.disconnect()


async def _probe(device: Device, interface: MqttInterface):
    try:
        device.firmware = await interface.detect_firmware()
    except (UnsupportedFirmware, MiniconfError) as e:
        device.error = str(e)
    except (ConnectionError, TimeoutError):
        device.error = "No answer"
    except Exception as e:
        # One device must not take the others on the broker down with it.
        logger.exception("Failed to probe %s", device)
        device.error = f"{type(e).__name__}: {e}"
