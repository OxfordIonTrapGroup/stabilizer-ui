# Self-references just work with new string-stored annotations.
from __future__ import annotations

import asyncio
import json
import logging
import websockets

logger = logging.getLogger(__name__)


class Solstis:
    @classmethod
    async def new(cls, server, port=8088, timeout=10) -> Solstis:
        """
        Intended to be short-lived.
        """
        # Connect to control page URL to get the page_start message.
        uri = f"ws://{server}:{port}/control.htm"

        # Disable pings, as the ICE-Bloc firmware doesn't implement them in a
        # standards-compliant way.
        socket = await websockets.connect(uri, ping_interval=None)
        logger.info("Connected to ICE-Bloc.")

        receive_queue = asyncio.Queue()

        async def receive_loop():
            async for message in socket:
                await receive_queue.put(message)

        receive_task = asyncio.create_task(receive_loop())

        result = Solstis(socket, timeout, receive_queue, receive_task)
        while not result._initialised:
            await result._process_next()
        return result

    def __init__(self, socket: websockets.WebSocketClientProtocol, timeout: float,
                 receive_queue, receive_task):
        self._socket = socket
        self._etalon_tune = None
        self._resonator_tune = None
        self._initialised = False
        self._timeout = timeout
        self._receive_queue = receive_queue
        self._receive_task = receive_task

    async def close(self):
        await self._socket.close()
        try:
            self._receive_task.cancel()
            await self._receive_task
        except (asyncio.CancelledError, websockets.exceptions.ConnectionClosedError):
            pass
        self._initialised = False

    async def _process_next(self):
        raw_msg = await asyncio.wait_for(self._receive_queue.get(),
                                         timeout=self._timeout)
        try:
            msg = json.loads(raw_msg)
        except json.JSONDecodeError:
            logger.warning("Error decoding JSON from ICE-Bloc: %s", raw_msg)
            return False

        typ = msg["message_type"]
        if typ == "page_start":
            # Unfortunately, page_start turns out to be the only way to get at the
            # tune parameters (at least from the messages used by the browser page).
            # control_page_result has a different set of data.
            self._etalon_tune = msg["etalon_tune"]
            self._resonator_tune = msg["resonator_tune"]
            self._etalon_locked = msg["etalon_lock_status"] == 1
            self._initialised = True
            return False

        if typ == "blocked_message":
            raise RuntimeError(msg.get("block_message", "Request blocked"))

        if typ == "control_page_result":
            # Treat next control_page_result as evidence of successful completion.
            return True

        return False

    async def _send(self, msg, blind=False):
        try:
            while True:
                self._receive_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        await self._socket.send(json.dumps(msg, separators=(",", ":")))
        if blind:
            return
        while (not await self._process_next()):
            pass

    @property
    def etalon_tune(self):
        assert self._initialised, "Connection closed"
        return self._etalon_tune

    async def set_etalon_tune(self, tune):
        if tune < 0 or tune > 100:
            raise ValueError(f"Invalid etalon tuning value: {tune}")
        self._etalon_tune = tune
        await self._send({
            "task": ["job_set_etalon_tuning"],
            "etalon_tune": tune,
            "message_type": "page_update"
        })

    @property
    def etalon_locked(self):
        assert self._initialised, "Connection closed"
        return self._etalon_locked

    async def set_etalon_locked(self, lock):
        self._etalon_locked = lock
        await self._send({
            "message_type": "task_request",
            "task": [f"job_etalon_lock_{'apply' if lock else 'remove'}"]
        })

    @property
    def resonator_tune(self):
        assert self._initialised, "Connection closed"
        return self._resonator_tune

    async def set_resonator_tune(self, tune, blind=False):
        if tune < 0 or tune > 100:
            raise ValueError(f"Invalid resonator tuning value: {tune}")
        self._resonator_tune = tune
        await self._send(
            {
                "task": ["job_set_resonator_tuning"],
                "resonator_tune": tune,
                "message_type": "page_update"
            },
            blind=blind,
        )


async def main():
    async def connect():
        solstis = await Solstis.new("localhost")
        print("Current etalon tune:", solstis.etalon_tune)
        print("Current resonator tune:", solstis.resonator_tune)
        print("Etalon currently locked:", solstis.etalon_locked)
        return solstis

    solstis = await connect()
    await solstis.set_etalon_locked(False)
    await solstis.set_etalon_tune(38)
    await solstis.close()

    solstis = await connect()
    await solstis.close()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
