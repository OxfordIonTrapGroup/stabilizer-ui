from __future__ import annotations
import asyncio
import time
import threading
import logging
import subprocess

from collections import deque, namedtuple
from typing import Callable
from PyQt5 import QtWidgets

from . import MAX_BUFFER_PERIOD
from ..mqtt import NetworkAddress
from ..utils import AsyncQueueThreadsafe

from stabilizer.stream import StabilizerStream, Parser, wrap
import numpy as np

logger = logging.getLogger(__name__)

CallbackPayload = namedtuple("CallbackPayload", "values download loss")


def launch_external_long_fft_scope(sample_period: float, opts: list = []):
    
    def _scope_launcher():
        """Launches the stabilizer-stream scope for long FFTs in an external window."""
        logger.info("Launching external long FFT scope")
        try:
            scopeProcessStatus = subprocess.Popen(
                [
                    "cargo", "run", "--bin", "psd", "--release", "--",
                    *opts
                ],
                cwd="./stabilizer_ui/stream/long-scope",
                stderr=subprocess.PIPE)
            (stdout_data, stderr_data) = scopeProcessStatus.communicate()

            if stdout_data is not None:
                logger.info(f"stabilizer-stream: {stdout_data.decode('utf-8')}")
            if stderr_data is not None:
                logger.error(f"stabilizer-stream: {stderr_data.decode('utf-8')}")

            # User closing the scope returns code 1, throw an error otherwise.
            if scopeProcessStatus.returncode not in [0, 1]:
                raise OSError
        except (FileNotFoundError, OSError):
            errorMsgBox = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Warning,
                                                "Error launching scope",
                                                "Unable to launch external scope.",
                                                QtWidgets.QMessageBox.Ok)
            errorMsgBox.setInformativeText(
                "The digital scope \'stabilizer-stream\' could not be opened. \
                    Check that the submodule has been cloned and Rust is installed.")
            errorMsgBox.exec()

    return _scope_launcher

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

        fftScopeWidget.longFftButton.clicked.connect(launch_external_long_fft_scope(fftScopeWidget.long_scope_options))

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
            ),
        )

    def start(self):
        self._thread.start()

    def close(self):
        self._terminate.set()
        self._thread.join()


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

        transport, stream = await StabilizerStream.open(stream_target.get_ip(),
                                                        stream_target.port,
                                                        broker_address.get_ip(), [parser],
                                                        maxsize=1)

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

    tasks = asyncio.gather(handle_callback(), handle_stream())
    new_loop.run_until_complete(tasks)
    new_loop.close()
