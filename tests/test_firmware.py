"""Translating the settings for earlier firmware, and detecting the version."""
import asyncio
import json

import pytest

from stabilizer_ui.firmware import (CURRENT, VERSION_NOT_PUBLISHED, FirmwareV09, Metadata,
                                    by_name, describe, describe_details)
from stabilizer_ui.mqtt import MiniconfError, MqttInterface, UnsupportedFirmware

V09 = FirmwareV09()

BIQUAD = {
    "coeff": {
        "ba": [0.5, -0.25, 0.0, 1.0, -0.125]
    },
    "u": 3.0,
    "min": -10,
    "max": 20
}


@pytest.mark.parametrize("key, topic", [
    ("settings/stream", "settings/stream_target"),
    ("settings/ch/1/gain", "settings/afe/1"),
    ("settings/ch/0/biquad/1/repr/Raw", "settings/iir_ch/0/1"),
    ("settings/ch/1/pounder/frequency_dds_out", "settings/pounder/1/frequency_dds_out"),
    ("settings/telemetry_period", "settings/telemetry_period"),
    ("ui/ch0/iir0", "ui/ch0/iir0"),
    ("alive", "alive"),
])
def test_v09_topics(key, topic):
    assert V09.topic(key) == topic
    assert V09.key(topic) == key
    assert CURRENT.topic(key) == key


@pytest.mark.parametrize("key", [
    "settings/ch/0/run", "settings/ch/0/biquad/0/typ", "settings/trigger",
    "settings/ch/1/source/amplitude", "settings/feedforward_harmonics/0"
])
def test_v09_unavailable(key):
    assert V09.topic(key) is None
    assert not V09.has(key)
    assert CURRENT.has(key)


@pytest.mark.parametrize("topic",
                         ["settings/force_hold", "settings/signal_generator/0/amplitude"])
def test_v09_settings_without_key(topic):
    assert V09.key(topic) is None


def test_v09_values():
    raw = "settings/ch/0/biquad/0/repr/Raw"
    device = V09.to_device(raw, BIQUAD)
    # idsp 0.15 has the opposite sign of a1 and a2.
    assert device == {
        "ba": [0.5, -0.25, 0.0, -1.0, 0.125],
        "u": 3.0,
        "min": -10,
        "max": 20
    }
    assert V09.from_device(raw, device) == BIQUAD

    stream = V09.to_device("settings/stream", "10.255.6.12:9293")
    assert stream == {"ip": [10, 255, 6, 12], "port": 9293}
    assert V09.from_device("settings/stream", stream) == "10.255.6.12:9293"

    assert V09.to_device("settings/telemetry_period", 9.6) == 10
    assert V09.to_device("settings/ch/0/gain", "G2") == "G2"
    assert CURRENT.to_device(raw, BIQUAD) is BIQUAD


def test_by_name():
    assert by_name("v0.9").name == "v0.9"
    assert by_name("v0.11") is CURRENT
    with pytest.raises(ValueError):
        by_name("v0.10")


class Device:
    """A stub MQTT client which answers the requests as a device with the settings
    `values` (by topic) would."""

    def __init__(self, values):
        self.values = values
        self.requests = []

    def subscribe(self, topic):
        pass

    def publish(self, topic, payload, **properties):
        topic = topic.split("/", 1)[1]
        self.requests.append((topic, payload))
        if topic in self.values:
            code, response = "Ok", json.dumps(self.values[topic]).encode()
        else:
            code, response = "Error", b"The provided path was not found (Key level: 1)"
        asyncio.get_running_loop().call_soon(
            self.on_message, self, properties["response_topic"], response, 0, {
                "correlation_data": [properties["correlation_data"]],
                "user_property": [("code", code)]
            })


@pytest.mark.parametrize("values, expected", [
    ({
        "settings/stream": "0.0.0.0:0"
    }, "v0.11"),
    ({
        "settings/stream_target": {
            "ip": [0, 0, 0, 0],
            "port": 0
        }
    }, "v0.9"),
])
def test_detect(values, expected):

    async def run():
        device = Device(values | {"settings/afe/0": "G2", "settings/ch/0/gain": "G5"})
        interface = MqttInterface(device, "device", 1)
        interface.subscribe()
        firmware = await interface.detect_firmware()
        assert firmware.name == expected
        assert interface.firmware is firmware
        # Requests now go to the topics of that version.
        gain = await interface.get("settings/ch/0/gain")
        assert gain == ("G2" if expected == "v0.9" else "G5")
        if expected == "v0.9":
            with pytest.raises(MiniconfError):
                await interface.request("settings/ch/0/run", "Hold")
            assert device.requests[-1][0] == "settings/afe/0"

    asyncio.run(run())


def test_detect_unknown():

    async def run():
        interface = MqttInterface(Device({}), "device", 1)
        interface.subscribe()
        with pytest.raises(UnsupportedFirmware):
            await interface.detect_firmware()
        assert interface.firmware is CURRENT

    asyncio.run(run())


def metadata(version, **values):
    """The metadata as the device publishes it, for firmware `version`."""
    return Metadata.parse(
        json.dumps({
            "firmware_version": version,
            "rust_version": "rustc 1.98.0",
            "profile": "release",
            "git_dirty": False,
            "features": "",
            "panic_info": "None",
            "hardware_version": "Rev1_3"
        } | values).encode())


@pytest.mark.parametrize(
    "version, expected",
    [
        ("v0.11.0-py-337-g693ca709", "v0.11"),
        ("v0.11.0", "v0.11"),
        ("v0.12.0-3-g0123abcd", "v0.11"),
        ("v0.9.0-221-g43064e14", "v0.9"),
        # Upstream, and the old `l674` lock firmware (miniconf 0.5).
        ("v0.10.0", None),
        ("v0.7.0-44-g8a7391ba", None),
        # Without a tag before the commit, or without git.
        ("693ca709", None),
        ("Unspecified", None),
    ])
def test_metadata_firmware(version, expected):
    firmware = metadata(version).firmware
    assert (firmware and firmware.name) == expected
    if firmware is not None:
        assert firmware is by_name(expected)


def test_metadata_values():
    meta = metadata("v0.11.0-py-337-g693ca709")
    assert str(meta) == "v0.11.0-py-337-g693ca709"
    assert meta.panic_info is None
    assert meta.values["hardware_version"] == "Rev1_3"

    meta = metadata("v0.11.0-py-337-g693ca709",
                    git_dirty=True,
                    panic_info="panicked at src/bin/dual-iir.rs:1:1")
    assert str(meta) == "v0.11.0-py-337-g693ca709-dirty"
    assert meta.panic_info == "panicked at src/bin/dual-iir.rs:1:1"
    assert meta.firmware is CURRENT

    # What the device publishes if the metadata does not fit its buffer.
    meta = Metadata.parse(b'{"message":"Truncated: See USB terminal"}')
    assert meta.version == ""
    assert meta.firmware is None

    for payload in [b"", b"OK", b'"v0.11.0"', b"[1]"]:
        with pytest.raises(ValueError):
            Metadata.parse(payload)


def test_describe():
    v09 = by_name("v0.9")
    assert describe(CURRENT, metadata("v0.11.0-py-337-g693ca709")) == \
        "v0.11.0-py-337-g693ca709"
    assert describe(v09, metadata("v0.9.0-221-g43064e14", git_dirty=True)) == \
        "v0.9.0-221-g43064e14-dirty"
    # The version does not tell the layout: it was found by asking the device.
    assert describe(CURRENT, metadata("Unspecified")) == "Unspecified (v0.11-ish)"
    assert describe(None, metadata("v0.10.0")) == "v0.10.0"
    # Only the layout is known, not the release.
    assert describe(v09, None) == "v0.9-ish"
    assert describe(CURRENT, Metadata.parse(b'{"message":"Truncated"}')) == "v0.11-ish"
    assert describe(None, None) == "unknown"


def test_describe_details():
    """Only a firmware without metadata needs explaining (restarting the device tells)."""
    assert describe_details(CURRENT, None) == VERSION_NOT_PUBLISHED
    assert describe_details(CURRENT, metadata("v0.11.0-py-337-g693ca709")) == ""
    assert describe_details(CURRENT, metadata("Unspecified")) == ""
    assert describe_details(None, None) == ""


@pytest.mark.parametrize("version, expected", [
    ("v0.11.0-py-337-g693ca709", "v0.11"),
    ("v0.9.0-221-g43064e14", "v0.9"),
])
def test_detect_from_metadata(version, expected):
    """A version the metadata tells needs no request."""

    async def run():
        device = Device({})
        interface = MqttInterface(device, "device", 1)
        interface.subscribe()
        firmware = await interface.detect_firmware(metadata(version))
        assert firmware is by_name(expected)
        assert interface.firmware is firmware
        assert device.requests == []

    asyncio.run(run())


def test_detect_unknown_metadata():
    """Otherwise the device is asked."""

    async def run():
        device = Device({"settings/stream_target": {"ip": [0, 0, 0, 0], "port": 0}})
        interface = MqttInterface(device, "device", 1)
        interface.subscribe()
        firmware = await interface.detect_firmware(metadata("Unspecified"))
        assert firmware is by_name("v0.9")
        assert [topic for topic, _ in device.requests
                ] == ["settings/stream", "settings/stream_target"]

    asyncio.run(run())
