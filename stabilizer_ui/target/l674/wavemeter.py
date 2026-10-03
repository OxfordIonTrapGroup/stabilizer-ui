"""The WAnD wavemeter server, over the sipyco RPC."""
import asyncio
import logging

logger = logging.getLogger(__name__)


class WavemeterInterface:
    """Wraps a connection to the WAnD wavemeter server, offering an interface to query
    a single channel while automatically reconnecting on failure/timeout.
    """

    def __init__(self, host: str, port: int, channel: str, timeout: float):
        self._client = None
        self._host = host
        self._port = port
        self._channel = channel
        self._timeout = timeout

    def __str__(self):
        return f"{self._host}:{self._port} ({self._channel})"

    async def try_connect(self) -> None:
        try:
            # Only needed for relocking.
            from sipyco import pc_rpc
        except ImportError:
            logger.error("sipyco is not installed; no wavemeter connection")
            self._client = None
            return
        try:
            self._client = pc_rpc.AsyncioClient()
            await asyncio.wait_for(self._client.connect_rpc(self._host, self._port,
                                                            "control"),
                                   timeout=self._timeout)
        except asyncio.CancelledError:
            await self.close()
            raise
        except (OSError, EOFError, asyncio.TimeoutError) as e:
            # Expected while the server is down; retried by `get_freq_offset()`.
            logger.warning("Failed to connect to WAnD server at %s:%s: %r", self._host,
                           self._port, e)
            self._client = None
        except Exception:
            logger.exception("Failed to connect to WAnD server at %s:%s", self._host,
                             self._port)
            self._client = None

    def is_connected(self) -> bool:
        return self._client is not None

    async def close(self):
        if self._client is not None:
            client, self._client = self._client, None
            try:
                await client.close_rpc()
            except Exception:
                pass

    async def get_freq_offset(self, age=0) -> tuple:
        """The reading of the channel as `(status, frequency, osa)`, with the frequency as
        an offset from the reference of the channel in Hz (`offset_mode`). Blocks until a
        reading is obtained, reconnecting as needed."""
        while True:
            while not self.is_connected():
                logger.info("Reconnecting to WAnD server")
                await self.try_connect()
                if not self.is_connected():
                    await asyncio.sleep(1)

            try:
                return await asyncio.wait_for(self._client.get_freq(laser=self._channel,
                                                                    age=age,
                                                                    priority=10,
                                                                    offset_mode=True),
                                              timeout=self._timeout)
            except asyncio.CancelledError:
                raise
            except (OSError, EOFError, asyncio.TimeoutError) as e:
                logger.warning("Error getting %s wavemeter reading: %r", self._channel, e)
                await self.close()
                continue
            except Exception:
                logger.exception(f"Error getting {self._channel} wavemeter reading")
                # Drop connection (to later reconnect). In regular operation, about the
                # only reason this should happen is due to timeouts after server
                # restarts/network weirdness, so don't bother distinguishing between
                # error types.
                await self.close()
