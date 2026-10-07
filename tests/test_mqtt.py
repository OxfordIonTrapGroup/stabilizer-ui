"""Session failures and rendering must never turn stale state into a new write."""
import asyncio
import json
import os

import pytest

from stabilizer_ui.firmware import FIRMWARES, by_name
from stabilizer_ui.mqtt import (MiniconfError, MqttInterface, UiMqttBridge, UiMqttConfig,
                                UnsupportedFirmware)


class Client:

    def __init__(self):
        self.subscriptions = []
        self.messages = []

    def subscribe(self, topic):
        self.subscriptions.append(topic)

    def publish(self, topic, payload, **properties):
        self.messages.append((topic, payload, properties))

    def reply(self, index, value):
        self.reply_raw(index, json.dumps(value).encode())

    def reply_raw(self, index, payload, code='Ok'):
        _, _, properties = self.messages[index]
        self.on_message(
            self, properties['response_topic'], payload, 0, {
                'correlation_data': [properties['correlation_data']],
                'user_property': [('code', code)]
            })

    async def wait_for_request(self, count):
        while len(self.messages) < count:
            await asyncio.sleep(0)


def test_timeout_aborts_session_and_late_reply_cannot_complete_new_request():

    async def run():
        client = Client()
        bridge = UiMqttBridge(client, {})
        errors = []
        bridge.on_disconnect = errors.append
        interface = MqttInterface(client, 'device', 0.02, on_error=bridge.interrupt)
        bridge._interface = interface
        bridge._connected()
        interface.subscribe()
        bridge.queue_write('settings/gain')

        requests = [
            asyncio.create_task(interface.get('settings/gain')),
            asyncio.create_task(interface.request('settings/run', 'Run', timeout=1))
        ]
        results = await asyncio.gather(*requests, return_exceptions=True)
        assert all(isinstance(result, ConnectionError) for result in results)
        assert len(errors) == 1
        assert not bridge.connected.is_set()
        assert not bridge.keys_to_write
        assert not interface._pending

        bridge._connected()
        interface.subscribe()
        fresh = asyncio.create_task(interface.get('settings/gain'))
        await asyncio.sleep(0)
        client.reply(0, 'G1')  # Response from the abandoned session.
        assert not fresh.done()
        client.reply(2, 'G5')
        assert await fresh == 'G5'
        assert len(client.subscriptions) == 2
        assert not interface._pending

    asyncio.run(run())


def test_get_answered_without_a_value_is_a_device_error():
    """Firmware too old for gets answers the empty payload with `OK` (it takes it as a
    set), which must not crash the caller but count as an unsupported firmware."""

    async def run():
        client = Client()
        interface = MqttInterface(client, 'device', 1)
        interface.subscribe()

        request = asyncio.create_task(interface.get('settings/stream'))
        await client.wait_for_request(1)
        client.reply_raw(0, b'OK')
        with pytest.raises(MiniconfError, match="Not a value: 'OK'"):
            await request
        assert not interface._pending

        detect = asyncio.create_task(interface.detect_firmware())
        for i in range(len(FIRMWARES)):
            await client.wait_for_request(i + 2)
            client.reply_raw(i + 1, b'OK')
        with pytest.raises(UnsupportedFirmware):
            await detect
        assert not interface._pending

    asyncio.run(run())


def test_disconnect_wakes_idle_worker_and_aborts_pending_request():

    async def run():
        client = Client()
        bridge = UiMqttBridge(client, {})
        interface = MqttInterface(client, 'device', 10)
        bridge._interface = interface
        interface.subscribe()
        bridge._connected()
        bridge.updated.clear()
        request = asyncio.create_task(interface.get('settings/gain'))
        await asyncio.sleep(0)
        client.on_disconnect(client, b'')
        with pytest.raises(ConnectionError):
            await request
        assert bridge.updated.is_set()
        assert not bridge.connected.is_set()
        assert not interface._pending

    asyncio.run(run())


def test_cancelled_request_is_removed():

    async def run():
        client = Client()
        interface = MqttInterface(client, 'device', 10)
        request = asyncio.create_task(interface.get('settings/gain'))
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not interface._pending
        client.reply(0, 'G2')

    asyncio.run(run())


def test_metadata_follows_the_device():
    """The build metadata is followed as the device publishes it (retained or not),
    until someone clears it."""

    class Window:

        def __init__(self):
            self.metadata = []

        def set_firmware_metadata(self, metadata):
            self.metadata.append(metadata)

    bridge = UiMqttBridge(Client(), {})
    window = Window()
    bridge._root_topic = 'device'
    bridge._ui = window
    payload = json.dumps({
        'firmware_version': 'v0.9.0-221-g43064e14',
        'panic_info': 'panicked at src/hardware/mod.rs:1:1'
    }).encode()
    bridge.handle_message('device/meta', payload, {'retain': False})
    assert bridge.metadata.firmware is by_name('v0.9')
    assert bridge.panicked
    assert window.metadata == [bridge.metadata]

    bridge.handle_message('device/meta', b'Truncated', {'retain': True})
    assert bridge.metadata.firmware is by_name('v0.9')

    bridge.handle_message('device/meta', b'', {'retain': False})
    assert bridge.metadata is None
    assert window.metadata[-1] is None

    bridge.handle_message('device/meta', b'{"firmware_version": "v0.11.0"}',
                          {'retain': True})
    assert bridge.metadata.firmware is by_name('v0.11')
    assert not bridge.panicked


@pytest.fixture(scope='module')
def application():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_rapid_edits_are_coalesced_and_incoming_values_do_not_overwrite_them(application):
    from PyQt6.QtWidgets import QDoubleSpinBox
    widget = QDoubleSpinBox()
    bridge = UiMqttBridge(Client(), {'ui/value': UiMqttConfig([widget])})
    bridge.connect_ui()
    for value in range(1, 50):
        widget.setValue(value)
    bridge._handle_ui_message('ui/value', b'10', {}, False)
    assert bridge.keys_to_write == {'ui/value'}
    assert widget.value() == 49

    key = bridge.keys_to_write.pop()
    assert bridge.configs[key].read_handler([widget]) == 49
    # Once sent, our echo and subsequent clients' values follow broker order.
    bridge._handle_ui_message(key, b'49', {}, False)
    bridge._handle_ui_message(key, b'51', {}, False)
    assert widget.value() == 51
    assert not bridge.keys_to_write


def test_fnc_readback_does_not_apply_linked_dds_calculation(application):
    from stabilizer_ui.target.fnc.ui import ChannelSettings
    from stabilizer_ui.utils import mega
    channel = ChannelSettings()
    bridge = UiMqttBridge(
        Client(), {
            'input': UiMqttConfig([channel.ddsInFrequencyBox], *mega),
            'output': UiMqttConfig([channel.ddsOutFrequencyBox], *mega),
            'link': UiMqttConfig([channel.ddsIoFreqLinkCheckBox]),
        })
    bridge.connect_ui()
    bridge.show('input', 150e6)
    bridge.show('output', 90e6)
    bridge.show('link', False)
    bridge.show('link', True)
    assert channel.ddsInFrequencyBox.value() == 150
    assert channel.ddsOutFrequencyBox.value() == 90
    assert not bridge.keys_to_write
    channel.ddsOutFrequencyBox.setValue(80)
    assert channel.ddsInFrequencyBox.value() == 160
    assert bridge.keys_to_write == {'input', 'output'}
