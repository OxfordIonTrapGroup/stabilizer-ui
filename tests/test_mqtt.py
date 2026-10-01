"""Session failures and rendering must never turn stale state into a new write."""
import asyncio
import json
import os

import pytest

from stabilizer_ui.mqtt import MqttInterface, UiMqttBridge, UiMqttConfig


class Client:

    def __init__(self):
        self.subscriptions = []
        self.messages = []

    def subscribe(self, topic):
        self.subscriptions.append(topic)

    def publish(self, topic, payload, **properties):
        self.messages.append((topic, payload, properties))

    def reply(self, index, value):
        _, _, properties = self.messages[index]
        self.on_message(
            self, properties['response_topic'],
            json.dumps(value).encode(), 0, {
                'correlation_data': [properties['correlation_data']],
                'user_property': [('code', 'Ok')]
            })


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
    bridge._handle_ui_message('ui/value', b'10', {})
    assert bridge.keys_to_write == {'ui/value'}
    assert widget.value() == 49

    key = bridge.keys_to_write.pop()
    assert bridge.configs[key].read_handler([widget]) == 49
    # Once sent, our echo and subsequent clients' values follow broker order.
    bridge._handle_ui_message(key, b'49', {})
    bridge._handle_ui_message(key, b'51', {})
    assert widget.value() == 51
    assert not bridge.keys_to_write
