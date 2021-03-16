import asyncio
from contextlib import suppress
from gmqtt import Client
from typing import Any
import logging
import json
import uuid

logger = logging.getLogger(__name__)


def _int_to_bytes(i):
    return i.to_bytes(i.bit_length() // 8 + 1, byteorder="little")


def starts_with(string, prefix) -> bool:
    return len(string) >= len(prefix) and string[:len(prefix)] == prefix


class MqttInterface:
    """
    Wraps a gmqtt Client to provide a request/response-type interface using the MQTT 5
    response topics/correlation data machinery.

    A timeout is also applied to every request (which is necessary for robustness, as
    Stabilizer only supports QoS 0 for now).
    """
    def __init__(self, client: Client, topic_base: str, timeout: float):
        self._client = client
        self._topic_base = topic_base
        self._pending = {}
        self._timeout = timeout

        #: Use incrementing sequence id as correlation data to map responses to requests
        #: (together with client id).
        self._next_seq_id = 0

        # Generate a random client ID (no real reason to use UUID here over another
        # source of randomness).
        client_id = str(uuid.uuid4()).split("-")[0]
        self._response_base = f"{topic_base}/response_{client_id}"
        self._client.subscribe(f"{self._response_base}/#")

        self._client.on_message = self._on_message

    async def request(self, topic: str, argument: Any, retain: bool = False):
        result = asyncio.Future()
        correlation_data = _int_to_bytes(self._next_seq_id)

        self._pending[correlation_data] = result

        payload = json.dumps(argument).encode("utf-8")
        self._client.publish(f"{self._topic_base}/{topic}",
                             payload,
                             qos=0,
                             retain=retain,
                             response_topic=f"{self._response_base}/{topic}",
                             correlation_data=correlation_data)
        self._next_seq_id += 1

        async def fail_after_timeout():
            await asyncio.sleep(self._timeout)
            result.set_exception(
                TimeoutError(f"No response to {topic} request after {self._timeout} s"))
            self._pending.pop(correlation_data)

        _, pending = await asyncio.wait(
            [result, asyncio.create_task(fail_after_timeout())],
            return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
            with suppress(asyncio.CancelledError):
                await p
        return await result

    def _on_message(self, _client, topic, payload, _qos, properties) -> int:
        if not starts_with(topic, self._response_base):
            logger.debug("Ignoring unrelated topic: %s", topic)
            return 0

        cd = properties.get("correlation_data", None)
        if cd is None:
            logger.warning(
                "Received response without correlation data (topic '%s', payload %s)",
                topic, payload)
            return 0

        if cd not in self._pending:
            # This is fine if Stabilizer restarts, though.
            logger.warning("Received unexpected/late response for '%s' (id %s)", topic,
                           cd)
            return 0

        result = self._pending.pop(cd)
        if not result.cancelled():
            try:
                # Would like to json.loads() here, but the miniconf responses are
                # unfortunately plain strings still (see quartiq/miniconf#32).
                result.set_result(payload)
            except BaseException as e:
                err = ValueError(f"Failed to parse response for '{topic}'")
                err.__cause__ = e
                result.set_exception(err)
        return 0
