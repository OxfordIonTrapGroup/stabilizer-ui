from __future__ import annotations
import asyncio
import socket
import sys
import time
import threading
import logging
from collections import deque, namedtuple
from contextlib import suppress
from typing import Callable

from . import MAX_BUFFER_PERIOD
from ..mqtt import NetworkAddress
from ..utils import AsyncQueueThreadsafe

from stabilizer.stream import wrap
from stabilizer.stream_parser import StabilizerStream, Parser
import numpy as np

logger = logging.getLogger(__name__)

CallbackPayload = namedtuple("CallbackPayload", "values download loss")

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


class _CapturingStream(StabilizerStream):
    """`StabilizerStream` which also passes the raw frames to a capture (if set)."""

    capture: StreamCapture | None = None

    def datagram_received(self, data, addr):
        capture = self.capture
        if capture is not None:
            header = self.header._make(self.header_fmt.unpack_from(data))
            if header.magic == self.magic:
                capture.add(header, data[self.header_fmt.size:])
        super().datagram_received(data, addr)


class StreamThread:

    def __init__(self,
                 ui_callback: Callable,
                 fftScopeWidget: FftScope,
                 stream_target_queue: asyncio.Queue[NetworkAddress],
                 broker_address: NetworkAddress,
                 main_event_loop: asyncio.AbstractEventLoop,
                 max_buffer_period: float = MAX_BUFFER_PERIOD):

        parser = fftScopeWidget.stream_parser
        precondition_data = fftScopeWidget.precondition_data()
        callback_interval = fftScopeWidget.update_period
        maxlen = int(max_buffer_period / fftScopeWidget.sample_period)

        #: The stream protocol, once the socket is open.
        self._stream: _CapturingStream | None = None
        self.parser = parser
        self.sample_period = fftScopeWidget.sample_period

        def set_stream(stream):
            self._stream = stream

        self._terminate = threading.Event()
        self._thread = threading.Thread(
            target=stream_worker,
            args=(
                ui_callback,
                parser,
                precondition_data,
                callback_interval,
                stream_target_queue,
                broker_address,
                main_event_loop,
                self._terminate,
                maxlen,
                set_stream,
            ),
        )

    def start(self):
        self._thread.start()

    def close(self):
        self._terminate.set()
        self._thread.join()

    def start_capture(self, capture: StreamCapture):
        """Start passing all received frames to `capture`."""
        if self._stream is None:
            raise RuntimeError("Stream not open")
        self._stream.capture = capture

    def stop_capture(self):
        if self._stream is not None:
            self._stream.capture = None


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

    def update(self, frame: Parser):
        sequence = frame.header.sequence
        lost = 0 if self._expect is None else wrap(sequence - self._expect)
        batch_count = frame.header.batches
        self._expect = wrap(sequence + batch_count)
        bytes = frame.size()

        self._stat.append(_StatPoint(time.monotonic_ns(), batch_count, lost, bytes))

    @property
    def download(self):
        """Bytes per second"""
        duration = (self._stat[-1].time - self._stat[0].time + 1) / 1e9
        bytes = np.sum(s.bytes for s in self._stat)
        return bytes / duration

    @property
    def loss(self):
        """Fraction of batches lost"""
        received, lost = np.sum([[s.received, s.lost] for s in self._stat], axis=0)
        sent = received + lost
        return lost / sent if sent else 1


def stream_worker(
    ui_callback: Callable,
    parser: Parser,
    precondition_data: Callable,
    callback_interval: float,
    stream_target_queue: AsyncQueueThreadsafe[NetworkAddress],
    broker_address: NetworkAddress,
    main_loop: asyncio.AbstractEventLoop,
    terminate: threading.Event,
    maxlen: int,
    set_stream: Callable,
):
    """This function doesn't run in the main thread!

    The default loop on Windows doesn't support UDP!
    Also, it is not possible to change the Qt event loop. Therefore, we
    have to handle the stream in a separate thread running this function.
    """

    buffer = [deque(np.zeros(maxlen), maxlen=maxlen) for _ in range(parser.n_sources)]
    stat = StreamStats()

    async def handle_stream():
        """This coroutine doesn't run in the main thread's loop!

        We first get the stream target from the queue, and queue back the allocated
        port for streaming. 

        The stream is then processed until it is requested to terminate.
        """
        stream_target = await stream_target_queue.get_threadsafe()
        stream_target_queue.task_done()
        logger.debug("Got initial requested stream target.")

        transport, stream = await _CapturingStream.open(stream_target.get_ip(),
                                                        stream_target.port,
                                                        broker_address.get_ip(), [parser],
                                                        maxsize=1)
        _enlarge_receive_buffer(transport.get_extra_info("socket"), RECEIVE_BUFFER_SIZE)
        set_stream(stream)

        allocated_stream_port = transport.get_extra_info("sockname")[1]
        stream_target = NetworkAddress(stream_target.ip, allocated_stream_port)

        logger.info(f"Binding stream to port: {allocated_stream_port}")
        await stream_target_queue.put_threadsafe(stream_target)
        # Wait for main thread to read the port
        logger.debug("StreamThread awaiting main thread to read stream target...")
        await stream_target_queue.join_threadsafe()
        logger.debug("StreamThread resuming...")

        try:
            while not terminate.is_set():
                frame = await stream.queue.get()
                stat.update(frame)
                for buf, values in zip(buffer, frame.to_si()):
                    buf.extend(values)
        finally:
            transport.close()

    async def handle_callback():
        """This coroutine doesn't run in the main thread's loop!"""
        while not terminate.is_set():
            while not all(map(len, buffer)):
                await asyncio.sleep(callback_interval)

            payload = CallbackPayload(
                precondition_data(parser.StreamData(*buffer)),
                stat.download,
                stat.loss,
            )

            main_loop.call_soon_threadsafe(ui_callback, payload)
            # Do not overload the main thread!
            await asyncio.sleep(callback_interval)

    async def _wait_for_main_loop():
        """Wait until main loop is running (can only return if it is running)
        This coroutine runs in the main thread's loop.
        """
        return True

    # Wait for the future to return.
    asyncio.run_coroutine_threadsafe(_wait_for_main_loop(), main_loop).result()

    new_loop = asyncio.SelectorEventLoop()
    # Setting the event loop here only applies locally to this thread.
    asyncio.set_event_loop(new_loop)

    async def run():
        stream_task = asyncio.ensure_future(handle_stream())

        async def callback():
            await handle_callback()
            # `handle_stream()` only notices the request to terminate when a frame
            # arrives, which might never happen (e.g. if the device streams to another
            # client).
            stream_task.cancel()

        with suppress(asyncio.CancelledError):
            await asyncio.gather(callback(), stream_task)

    new_loop.run_until_complete(run())
    new_loop.close()
