"""One device on a broker must not break the search for the others."""
import asyncio

from stabilizer_ui import discovery
from stabilizer_ui.firmware import CURRENT, Metadata
from stabilizer_ui.mqtt import NetworkAddress, UnsupportedFirmware


class Interface:

    def __init__(self, error):
        self.error = error

    async def detect_firmware(self):
        raise self.error


def test_probe_reports_errors_on_the_device_instead_of_raising():
    broker = NetworkAddress.from_str_ip('127.0.0.1', 1883)
    for error, expected in [
        (UnsupportedFirmware('Unknown firmware version'), 'Unknown firmware version'),
        (TimeoutError(), 'No answer'),
        (RuntimeError('boom'), 'RuntimeError: boom'),
    ]:
        device = discovery.Device(broker, 'dual-iir', 'aa-bb', alive=True)
        asyncio.run(discovery._probe(device, Interface(error)))
        assert device.firmware is None
        assert device.error == expected


def test_firmware_label():
    broker = NetworkAddress.from_str_ip('127.0.0.1', 1883)

    def label(app='dual-iir', **fields):
        return discovery.Device(broker, app, 'aa-bb', alive=True, **fields).firmware_label

    meta = Metadata.parse(b'{"firmware_version": "v0.11.0-py-1-g0123abcd"}')
    assert label(firmware=meta.firmware, metadata=meta) == 'v0.11.0-py-1-g0123abcd'
    assert label(firmware=CURRENT) == 'v0.11-ish'
    assert label() == 'unknown'
    assert label(error='No answer') == 'unknown (No answer)'
    unknown = Metadata.parse(b'{"firmware_version": "Unspecified"}')
    assert label(firmware=CURRENT, metadata=unknown) == 'Unspecified (v0.11-ish)'
    assert label(metadata=unknown, error='No answer') == 'Unspecified'
    # Applications without a UI are not asked, but their metadata can be shown.
    assert label('lockin') == '–'
    assert label('lockin', metadata=meta) == 'v0.11.0-py-1-g0123abcd'
