"""The firmware versions the UI supports, and the differences of the earlier ones.

The UI uses the settings tree and value formats of the current firmware (v0.11, the merge
of upstream quartiq/stabilizer) throughout. For an earlier version, its `Firmware` maps
these keys and values to those of the device, and back. Settings it has no counterpart
for are not available.
"""
from __future__ import annotations

import re
from typing import Any, Callable, NamedTuple, Optional


class Firmware:
    """The current firmware: settings are where the UI expects them."""

    #: Shown in the UI.
    name = "v0.11"

    #: The key of a setting which only this version has where it expects it (among the
    #: versions after it in `FIRMWARES`), read to detect the version.
    probe_key = "settings/stream"

    def topic(self, key: str) -> Optional[str]:
        """The topic (below the device prefix) of the setting `key`, or `None` if this
        firmware does not have it. Topics other than `settings/...` are the same."""
        return key

    def key(self, topic: str) -> Optional[str]:
        """The key of the setting at `topic` (below the device prefix), or `None`."""
        return topic

    def has(self, key: str) -> bool:
        return self.topic(key) is not None

    def to_device(self, key: str, value: Any) -> Any:
        """The value of the setting `key` as the device has it."""
        return value

    def from_device(self, key: str, value: Any) -> Any:
        """The value of the setting `key`, as the UI uses it, from the device's."""
        return value

    def __str__(self):
        return self.name


def _identity(value):
    return value


class _Rule(NamedTuple):
    """A setting (or several, with placeholders) at another place, or in another
    format."""
    key: re.Pattern
    topic: re.Pattern
    key_format: str
    topic_format: str
    to_device: Callable[[Any], Any]
    from_device: Callable[[Any], Any]


def _rule(key: str, topic: str, to_device=_identity, from_device=_identity) -> _Rule:
    """A `_Rule` mapping `key` to `topic`, both with placeholders (`{ch}` and `{idx}` for
    indices, `{name}` for a path element)."""

    def pattern(path):
        regex = re.escape(path)
        for name, group in [("ch", r"\d+"), ("idx", r"\d+"), ("name", r"[^/]+")]:
            regex = regex.replace(re.escape(f"{{{name}}}"), f"(?P<{name}>{group})")
        return re.compile(regex)

    return _Rule(pattern(key), pattern(topic), key, topic, to_device, from_device)


def _biquad_to_v09(value):
    # idsp 0.15 has the opposite sign of the feedback coefficients.
    b0, b1, b2, a1, a2 = value["coeff"]["ba"]
    return {
        "ba": [b0, b1, b2, -a1, -a2],
        "u": value["u"],
        "min": value["min"],
        "max": value["max"]
    }


def _biquad_from_v09(value):
    b0, b1, b2, a1, a2 = value["ba"]
    return {
        "coeff": {
            "ba": [b0, b1, b2, -a1, -a2]
        },
        "u": value["u"],
        "min": value["min"],
        "max": value["max"]
    }


def _stream_to_v09(value: str):
    ip, port = value.rsplit(":", 1)
    return {"ip": [int(byte) for byte in ip.split(".")], "port": int(port)}


def _stream_from_v09(value) -> str:
    return f"{'.'.join(map(str, value['ip']))}:{value['port']}"


class FirmwareV09(Firmware):
    """OITG `master` (43064e14, miniconf 0.9), as before the upstream merge.

    It has no signal source to sweep, and holds both channels by two global flags
    (`allow_hold`, `force_hold`) instead of a run mode per channel; neither is available.
    """

    name = "v0.9"

    _rules = [
        _rule("settings/stream", "settings/stream_target", _stream_to_v09,
              _stream_from_v09),
        _rule("settings/ch/{ch}/gain", "settings/afe/{ch}"),
        _rule("settings/ch/{ch}/biquad/{idx}/repr/Raw", "settings/iir_ch/{ch}/{idx}",
              _biquad_to_v09, _biquad_from_v09),
        # `fnc`
        _rule("settings/ch/{ch}/pounder/{name}", "settings/pounder/{ch}/{name}"),
        # Whole seconds.
        _rule("settings/telemetry_period", "settings/telemetry_period", round, float),
    ]

    def _match(self, path: str, by_key: bool):
        for rule in self._rules:
            match = (rule.key if by_key else rule.topic).fullmatch(path)
            if match:
                return rule, match.groupdict()
        return None, None

    def topic(self, key: str) -> Optional[str]:
        if not key.startswith("settings/"):
            return key
        rule, fields = self._match(key, by_key=True)
        return None if rule is None else rule.topic_format.format(**fields)

    def key(self, topic: str) -> Optional[str]:
        if not topic.startswith("settings/"):
            return topic
        rule, fields = self._match(topic, by_key=False)
        return None if rule is None else rule.key_format.format(**fields)

    def to_device(self, key: str, value: Any) -> Any:
        rule, _ = self._match(key, by_key=True)
        return value if rule is None else rule.to_device(value)

    def from_device(self, key: str, value: Any) -> Any:
        rule, _ = self._match(key, by_key=True)
        return value if rule is None else rule.from_device(value)


#: The supported versions, the current one first. The device runs the first one which
#: answers a get of its `probe_key`.
FIRMWARES = [Firmware(), FirmwareV09()]

CURRENT = FIRMWARES[0]


def by_name(name: str) -> Firmware:
    for firmware in FIRMWARES:
        if firmware.name == name:
            return firmware
    raise ValueError(f"Unknown firmware version: {name}")
