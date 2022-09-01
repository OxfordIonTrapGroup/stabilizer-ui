import asyncio
import time
import threading
from collections import deque, namedtuple
from dataclasses import dataclass
from typing import Callable

from . import MAX_BUFFER_PERIOD
from ..ui_mqtt_bridge import NetworkAddress

from stabilizer.stream import StabilizerStream
from stabilizer.stream import AdcDac, wrap
from stabilizer import DAC_VOLTS_PER_LSB, SAMPLE_PERIOD
import numpy as np

# Order is consistent with `AdcDac.to_mu()`.
StreamData = namedtuple("StreamData", "ADC0 ADC1 DAC0 DAC1")

CallbackPayload = namedtuple("CallbackPayload", "values download loss")


class StreamThread:
    def __init__(self,
                 ui_callback: Callable,
                 precondition_data: Callable,
                 callback_interval: float,
                 stream_target: NetworkAddress,
                 max_buffer_period: float = MAX_BUFFER_PERIOD):
        main_event_loop = asyncio.get_running_loop()
        self._terminate = threading.Event()
        maxlen = int(max_buffer_period / SAMPLE_PERIOD)
        self._thread = threading.Thread(
            target=stream_worker,
            args=(ui_callback, precondition_data, callback_interval, stream_target,
                  main_event_loop, self._terminate, maxlen),
        )

    def start(self):
        self._thread.start()

    def close(self):
        self._terminate.set()
        self._thread.join()


@dataclass
class StreamStats:
    """Periodically resetting stream statistics using
    non-overlapping windows of 1s duration.
    """
    expect = None
    received = 0
    lost = 0
    bytes = 0

    def __post_init__(self):
        self._start_ns = time.monotonic_ns()
        self._last_ns = -1

    def update(self, frame: AdcDac):
        now_ns = time.monotonic_ns()
        if now_ns - self._start_ns > 1e9:
            self.expect = None
            self.received = 0
            self.lost = 0
            self.bytes = 0
            self._start_ns = self._last_ns
        self._last_ns = now_ns

        if self.expect is not None:
            self.lost += wrap(frame.header.sequence - self.expect)
        batch_count = frame.batch_count()
        self.received += batch_count
        self.expect = wrap(frame.header.sequence + batch_count)
        self.bytes += frame.size()

    @property
    def download(self):
        """Bytes per second"""
        duration = (self._last_ns - self._start_ns + 1) / 1e9
        return self.bytes / duration

    @property
    def loss(self):
        """Fraction of batches lost"""
        sent = self.received + self.lost
        return self.lost / sent if sent else 1


def stream_worker(
    ui_callback: Callable,
    precondition_data: Callable,
    callback_interval: float,
    stream_target: NetworkAddress,
    main_loop: asyncio.AbstractEventLoop,
    terminate: threading.Event,
    maxlen: int,
):
    """This function doesn't run in the main thread!

    The default loop on Windows doesn't support UDP!
    Also, it is not possible to change the Qt event loop. Therefore, we
    have to handle the stream in a separate thread running this function.
    """
    buffer = [deque(maxlen=maxlen) for _ in range(4)]
    stat = StreamStats()

    async def handle_stream():
        """This coroutine doesn't run in the main thread's loop!"""
        transport, stream = await StabilizerStream.open(
            (stream_target.get_ip(), stream_target.port), 1)
        try:
            while not terminate.is_set():
                frame = await stream.queue.get()
                stat.update(frame)
                for buf, values in zip(buffer, frame.to_mu()):
                    buf.extend(values)
        finally:
            transport.close()

    async def handle_callback():
        """This coroutine doesn't run in the main thread's loop!"""
        while not terminate.is_set():
            while not all(map(len, buffer)):
                await asyncio.sleep(callback_interval)

            volts = [np.array(buf) * DAC_VOLTS_PER_LSB for buf in buffer]
            payload = CallbackPayload(
                precondition_data(StreamData(*volts)),
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

    tasks = asyncio.gather(handle_callback(), handle_stream())
    new_loop.run_until_complete(tasks)
    new_loop.close()
