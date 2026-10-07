"""The firmware versions the UI supports, and the differences of the earlier ones.

The UI uses the settings tree and value formats of the current firmware (v0.11, the merge
of upstream quartiq/stabilizer) throughout. For an earlier version, its `Firmware` maps
these keys and values to those of the device, and back. Settings it has no counterpart
for are not available.

Which version a device runs is told by the build metadata it publishes (`Metadata`), if
the version given there is known; otherwise, by asking the device for a setting which
only one version has at its place (`Firmware.probe_key`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple, Optional


class Firmware:
    """The current firmware: settings are where the UI expects them."""

    #: The release which introduced this layout; identifies it (e.g. in recorded data).
    name = "v0.11"

    #: The key of a setting which only this version has where it expects it (among the
    #: versions after it in `FIRMWARES`), read to detect the version.
    probe_key = "settings/stream"

    def has_release(self, release: tuple[int, int]) -> bool:
        """Whether the builds of `release` (major, minor) have this layout."""
        return release >= (0, 11)

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
        # Shown in the UI: the device runs some build with the layout of this release, not
        # necessarily the release itself.
        return f"{self.name}-ish"


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

    def has_release(self, release: tuple[int, int]) -> bool:
        return release == (0, 9)

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

#: The release at the start of a firmware version (`git describe`, e.g.
#: `v0.11.0-py-337-g693ca709`): major and minor version.
_RELEASE = re.compile(r"v?(\d+)\.(\d+)(?:\D|$)")


@dataclass
class Metadata:
    """The build metadata a device publishes on `meta` when it connects to the broker
    (retained since October 2026, not retained before)."""

    #: The firmware version: `git describe --tags` of the build (e.g.
    #: `v0.11.0-py-337-g693ca709`; the commit if there is no tag before it, `Unspecified`
    #: if built without git, empty if not given).
    version: str
    #: Whether the build had uncommitted changes.
    dirty: bool
    #: Everything the device published (`firmware_version`, `profile`,
    #: `hardware_version`, `panic_info`, ...).
    values: dict

    @classmethod
    def parse(cls, payload: bytes) -> Metadata:
        """The metadata in a `meta` message, raising `ValueError` if it is none."""
        values = json.loads(payload)
        if not isinstance(values, dict):
            raise ValueError(f"Not an object: {values!r}")
        version = values.get("firmware_version")
        return cls(version if isinstance(version, str) else "",
                   values.get("git_dirty") is True, values)

    @property
    def firmware(self) -> Optional[Firmware]:
        """The firmware (settings layout) of the version, or `None` if the version does
        not tell (or is not supported)."""
        match = _RELEASE.match(self.version)
        if match is None:
            return None
        release = int(match[1]), int(match[2])
        return next((firmware for firmware in FIRMWARES if firmware.has_release(release)),
                    None)

    @property
    def panic_info(self) -> Optional[str]:
        """What the device reported about its last panic, if it has panicked since it was
        powered on."""
        info = self.values.get("panic_info", "None")
        return None if info == "None" else str(info)

    def __str__(self):
        return f"{self.version}-dirty" if self.dirty else self.version


#: Explains a firmware shown by its layout alone (see `describe_details()`).
VERSION_NOT_PUBLISHED = (
    "The exact firmware version is not known, only its settings layout (found out by "
    "asking the device). Firmware built before October 2026 publishes its version only "
    "when the device connects to the broker, without keeping it there, so only a window "
    "which is open at that moment sees it. To see it, switch the device off, wait until "
    "its window has shown it as offline for ten seconds (noticing that it is off can "
    "take a minute and a half), and switch it on again.")


def describe(firmware: Optional[Firmware], metadata: Optional[Metadata]) -> str:
    """The firmware of a device to show: the version of the build if known (with the
    layout the UI uses for it if the version does not tell), else the layout (as
    `v0.11-ish`, see `describe_details()`)."""
    if metadata is None or not metadata.version:
        return str(firmware) if firmware is not None else "unknown"
    if firmware is None or metadata.firmware is firmware:
        return str(metadata)
    return f"{metadata} ({firmware})"


def describe_details(firmware: Optional[Firmware], metadata: Optional[Metadata]) -> str:
    """An explanation to show with `describe()` (e.g. as tooltip), or an empty string."""
    if firmware is not None and metadata is None:
        return VERSION_NOT_PUBLISHED
    return ""


def by_name(name: str) -> Firmware:
    for firmware in FIRMWARES:
        if firmware.name == name:
            return firmware
    raise ValueError(f"Unknown firmware version: {name}")
