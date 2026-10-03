"""Translating the settings for earlier firmware, and detecting the version."""
import asyncio
import json

import pytest

from stabilizer_ui.firmware import CURRENT, FirmwareV09, by_name
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
