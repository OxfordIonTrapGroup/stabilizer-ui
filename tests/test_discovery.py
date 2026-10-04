"""One device on a broker must not break the search for the others."""
import asyncio

from stabilizer_ui import discovery
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
