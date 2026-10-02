from __future__ import annotations
import asyncio
import socket
import sys
import time
import threading
import logging
from collections import deque, namedtuple
from contextlib import suppress
from typing import Callable, TYPE_CHECKING

from ..mqtt import NetworkAddress
from ..utils import AsyncQueueThreadsafe

from stabilizer.stream import wrap
from stabilizer.stream_parser import StabilizerStream
import numpy as np

if TYPE_CHECKING:
    from .fft_scope import FftScope, ScopeConfig

logger = logging.getLogger(__name__)

#: Data for the scope: the plot data (`None` if no data has arrived since the previous
#: payload), the stream statistics, and the `ScopeConfig` the data was computed for.
CallbackPayload = namedtuple("CallbackPayload", "values download loss config")

#: Interval at which the stream thread decodes the received frames, in seconds.
DECODE_INTERVAL = 0.02

#: Requested size of the UDP receive buffer, in bytes. The stream is about 6 MB/s for
#: four channels at 781 kHz, so this bridges a few seconds in which the stream thread
#: does not get to read the socket (e.g. while the main thread holds the GIL), which
#: captures (`StreamCapture`) depend on.
RECEIVE_BUFFER_SIZE = 32 << 20


def _enlarge_receive_buffer(sock: socket.socket, size: int):
    """Enlarge the receive buffer of `sock` to `size` bytes, or as far as the OS
    allows."""

    def current():
        value = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        # Linux reports twice the requested size, to account for its bookkeeping.
        return value // 2 if sys.platform == "linux" else value

    initial = current()
    request = size
    while request > initial:
        try:
            # Linux silently caps the size at `net.core.rmem_max`, whereas macOS
            # refuses sizes above (about) `kern.ipc.maxsockbuf`.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, request)
            break
        except OSError:
            request = request * 3 // 4
    actual = current()
    # Only warn about much smaller buffers, as the default limit is 8 MiB on macOS.
    if actual < size // 4:
        hint = {
            "linux": f" (raise it with `sysctl net.core.rmem_max={size}`)",
            "darwin": f" (raise it with `sysctl kern.ipc.maxsockbuf={2 * size}`)",
        }.get(sys.platform, "")
        logger.warning(
            "Stream receive buffer is %d kB instead of %d kB, which makes losing "
            "stream data more likely%s", actual >> 10, size >> 10, hint)
    else:
        logger.info("Stream receive buffer: %d kB", actual >> 10)


class StreamCapture:
    """Captures all stream frames (until a given length), for handing over to the main
    thread.

    Frames are added by the stream thread, whereas the other methods are to be used from
    the main thread (running `loop`).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, n_sources: int):
        self._loop = loop
        self._n_sources = n_sources
        #: The captured frames as (batch index, batch count, data).
        self._frames: list[tuple[int, int, bytes]] = []
        self._first_sequence = None
        self._target = None
        self._finished = False
        #: Number of batches captured (including lost ones).
        self.batches = 0
        #: Number of samples per batch (known once started).
        self.batch_size = None
        #: Set once the first frame has been received.
        self.started = asyncio.Event()
        #: Completed once the requested number of batches has been captured.
        self.done = loop.create_future()

    def add(self, header, body: bytes):
        """Add a frame (called from the stream thread)."""
        if self._finished:
            return
        if self._first_sequence is None:
            self._first_sequence = header.sequence
            self.batch_size = len(body) // (2 * self._n_sources * header.batches)
            self._loop.call_soon_threadsafe(self.started.set)
        position = wrap(header.sequence - self._first_sequence)
        if position >= 1 << 31:
            # Reordered frame from before the start of the capture.
            return
        self._frames.append((position, header.batches, body))
        self.batches = max(self.batches, position + header.batches)
        if self._target is not None and self.batches >= self._target:
            self._finished = True
            self._loop.call_soon_threadsafe(self._finish)

    def _finish(self):
        if not self.done.done():
            self.done.set_result(None)

    def stop_after(self, batches: int):
        """Finish the capture after the given number of further batches."""
        self._target = self.batches + batches

    @property
    def target(self) -> int | None:
        return self._target

    def assemble(self) -> tuple[np.ndarray, np.ndarray]:
        """Combine the captured frames.

        Returns the raw data as (source, sample) array, and the indices of the batches
        which were lost (and are filled with zeros).
        """
        if not self._frames:
            raise ValueError("No data captured")
        n_batches = self._target or self.batches
        n_sources, batch_size = self._n_sources, self.batch_size
        data = np.zeros((n_batches, n_sources, batch_size), np.int16)
        received = np.zeros(n_batches, bool)
        for position, batches, body in self._frames:
            stop = min(position + batches, n_batches)
            if stop <= position:
                continue
            frame = np.frombuffer(body, "<i2").reshape(batches, n_sources, batch_size)
            data[position:stop] = frame[:stop - position]
            received[position:stop] = True
        data = data.transpose(1, 0, 2).reshape(n_sources, -1)
        return data, np.flatnonzero(~received)


class _Stream(StabilizerStream):
    """`StabilizerStream` which collects the frames for decoding them in batches (instead
    of parsing and queueing each one), and also passes them to a capture (if set)."""

    capture: StreamCapture | None = None

    def __init__(self, maxsize, parsers):
        super().__init__(maxsize, parsers)
        #: The frames received since they were last taken, as (header, body).
        self.frames: list[tuple[tuple, bytes]] = []

    def datagram_received(self, data, _addr):
        header = self.header._make(self.header_fmt.unpack_from(data))
        if header.magic != self.magic:
            logger.warning("Bad frame magic: %#04x, ignoring", header.magic)
            return
        if header.format_id not in self.parsers:
            logger.warning("No parser for format %s, ignoring", header.format_id)
            return
        body = data[self.header_fmt.size:]
        capture = self.capture
        if capture is not None:
            capture.add(header, body)
        self.frames.append((header, body))


class _ScopeBuffer:
    """Ring buffer of the latest samples of each source."""

    def __init__(self, n_sources: int, length: int):
        self._data = np.zeros((n_sources, length), np.float32)
        #: Index of the oldest sample.
        self._start = 0
        #: Number of samples added in total.
        self.total = 0

    @property
    def length(self) -> int:
        return self._data.shape[1]

    def resize(self, length: int):
        """Change the length, keeping the latest samples."""
        latest = self.latest()
        self._data = np.zeros((len(latest), length), np.float32)
        n = min(length, latest.shape[1])
        self._data[:, length - n:] = latest[:, latest.shape[1] - n:]
        self._start = 0

    def add(self, data: np.ndarray):
        """Add samples, given as (source, sample) array."""
        self.total += data.shape[1]
        n = min(data.shape[1], self.length)
        data = data[:, data.shape[1] - n:]
        first = min(n, self.length - self._start)
        self._data[:, self._start:self._start + first] = data[:, :first]
        self._data[:, :n - first] = data[:, first:]
        self._start = (self._start + n) % self.length

    def latest(self) -> np.ndarray:
        """A copy of the data, from the oldest sample to the latest."""
        return np.concatenate([self._data[:, self._start:], self._data[:, :self._start]],
                              axis=1)


class StreamThread:
    """Receives the stream in a separate thread, and passes the data to the scope."""

    def __init__(self, ui_callback: Callable, fftScopeWidget: FftScope,
                 stream_target_queue: AsyncQueueThreadsafe[NetworkAddress],
                 broker_address: NetworkAddress,
                 main_event_loop: asyncio.AbstractEventLoop):
        self.parser = fftScopeWidget.stream_parser
        self.sample_period = fftScopeWidget.sample_period
        self._worker = _StreamWorker(ui_callback, fftScopeWidget, stream_target_queue,
                                     broker_address, main_event_loop)
        self._thread = threading.Thread(target=self._worker.run, name="stream")

    def start(self):
        self._thread.start()

    def close(self):
        self._worker.terminate.set()
        self._thread.join()

    def start_capture(self, capture: StreamCapture):
        """Start passing all received frames to `capture`."""
        stream = self._worker.stream
        if stream is None:
            raise RuntimeError("Stream not open")
        stream.capture = capture

    def stop_capture(self):
        if self._worker.stream is not None:
            self._worker.stream.capture = None


_StatPoint = namedtuple("_StatPoint", "time received lost bytes")


class StreamStats:
    """Moving average stream statistics

    :param maxlen: The number of retained historic points.
        Typically, there are 4000 updates per second.
    """

    def __init__(self, maxlen=4000):
        self._expect = None
        self._stat = deque(maxlen=maxlen)
        self._stat.append(_StatPoint(time.monotonic_ns(), 0, 0, 0))

    def update(self, header, size: int) -> int:
        """Add a frame, given its header and the size of its body, and return the number
        of batches lost before it."""
        sequence = header.sequence
        lost = 0 if self._expect is None else wrap(sequence - self._expect)
        if lost >= 1 << 31:
            # A reordered frame, or the device has restarted.
            lost = 0
        batch_count = header.batches
        self._expect = wrap(sequence + batch_count)

        self._stat.append(_StatPoint(time.monotonic_ns(), batch_count, lost, size))
        return lost

    @property
    def download(self):
        """Bytes per second"""
        duration = (self._stat[-1].time - self._stat[0].time + 1) / 1e9
        bytes = sum(s.bytes for s in self._stat)
        return bytes / duration

    @property
    def loss(self):
        """Fraction of batches lost"""
        received, lost = np.sum([[s.received, s.lost] for s in self._stat], axis=0)
        sent = received + lost
        return lost / sent if sent else 1


class _StreamWorker:
    """Receives and decodes the stream, and computes the scope data. Except for the
    constructor and `_show()`, this runs in the stream thread.

    The default loop on Windows doesn't support UDP!
    Also, it is not possible to change the Qt event loop. Therefore, we
    have to handle the stream in a separate thread.
    """

    def __init__(self, ui_callback: Callable, scope: FftScope,
                 stream_target_queue: AsyncQueueThreadsafe[NetworkAddress],
                 broker_address: NetworkAddress, main_loop: asyncio.AbstractEventLoop):
        self.ui_callback = ui_callback
        self.scope = scope
        self.parser = scope.stream_parser
        self.stream_target_queue = stream_target_queue
        self.broker_address = broker_address
        self.main_loop = main_loop
        self.terminate = threading.Event()
        #: The stream protocol, once the socket is open.
        self.stream: _Stream | None = None
        self._stats = StreamStats()
        self._buffer = _ScopeBuffer(self.parser.n_sources, scope.config.length)
        #: Clear while the main thread has not shown the latest scope data yet.
        self._shown = threading.Event()
        self._shown.set()

    def _decode(self):
        """Decode the frames received since the previous call into the scope buffer."""
        stream = self.stream
        if stream is None or not stream.frames:
            return
        frames, stream.frames = stream.frames, []
        for header, body in frames:
            self._stats.update(header, len(body))
        batches = sum(header.batches for header, _ in frames)
        try:
            # Decode all frames at once, as one with all their batches.
            header = frames[0][0]._replace(batches=batches)
            data = self.parser.set_frame(header, b"".join(body for _, body in frames))
            data = data.to_si().astype(np.float32)
        except ValueError:
            logger.exception("Failed to decode stream frames")
            return
        self._buffer.add(data)

    def _show(self, payload: CallbackPayload):
        """Show data in the scope (in the main thread)."""
        try:
            self.ui_callback(payload)
        finally:
            self._shown.set()

    async def _receive(self):
        """We first get the stream target from the queue, and queue back the allocated
        port for streaming.

        The stream is then processed until it is requested to terminate.
        """
        stream_target = await self.stream_target_queue.get_threadsafe()
        self.stream_target_queue.task_done()
        logger.debug("Got initial requested stream target.")

        transport, stream = await _Stream.open(stream_target.get_ip(),
                                               stream_target.port,
                                               self.broker_address.get_ip(),
                                               [self.parser],
                                               maxsize=1)
        _enlarge_receive_buffer(transport.get_extra_info("socket"), RECEIVE_BUFFER_SIZE)
        self.stream = stream

        allocated_stream_port = transport.get_extra_info("sockname")[1]
        stream_target = NetworkAddress(stream_target.ip, allocated_stream_port)

        logger.info(f"Binding stream to port: {allocated_stream_port}")
        await self.stream_target_queue.put_threadsafe(stream_target)
        # Wait for main thread to read the port
        logger.debug("StreamThread awaiting main thread to read stream target...")
        await self.stream_target_queue.join_threadsafe()
        logger.debug("StreamThread resuming...")

        try:
            while not self.terminate.is_set():
                self._decode()
                await asyncio.sleep(DECODE_INTERVAL)
        finally:
            transport.close()

    async def _update_scope(self):
        shown_total = None
        shown_config: ScopeConfig | None = None
        while not self.terminate.is_set():
            started = time.monotonic()
            # Wait for the main thread to show the previous data, not to overload it.
            if self._shown.is_set():
                self._decode()
                config = self.scope.config
                if config.length != self._buffer.length:
                    self._buffer.resize(config.length)
                values = None
                if self._buffer.total != shown_total or config is not shown_config:
                    shown_total, shown_config = self._buffer.total, config
                    # The FFT does not hold the GIL, nor block receiving the stream.
                    values = await asyncio.to_thread(config.precondition,
                                                     self._buffer.latest())
                payload = CallbackPayload(values, self._stats.download, self._stats.loss,
                                          config)
                self._shown.clear()
                self.main_loop.call_soon_threadsafe(self._show, payload)
            # Spend at most a third of the time on long traces.
            await asyncio.sleep(
                max(self.scope.update_period, 2 * (time.monotonic() - started)))

    def run(self):

        async def _wait_for_main_loop():
            """Wait until main loop is running (can only return if it is running)
            This coroutine runs in the main thread's loop.
            """
            return True

        # Wait for the future to return.
        asyncio.run_coroutine_threadsafe(_wait_for_main_loop(), self.main_loop).result()

        new_loop = asyncio.SelectorEventLoop()
        # Setting the event loop here only applies locally to this thread.
        asyncio.set_event_loop(new_loop)

        async def run():
            receive_task = asyncio.ensure_future(self._receive())

            async def update_scope():
                await self._update_scope()
                # `_receive()` might still be waiting for the main thread to exchange the
                # stream target.
                receive_task.cancel()

            with suppress(asyncio.CancelledError):
                await asyncio.gather(update_scope(), receive_task)

        new_loop.run_until_complete(run())
        new_loop.run_until_complete(new_loop.shutdown_default_executor())
        new_loop.close()
