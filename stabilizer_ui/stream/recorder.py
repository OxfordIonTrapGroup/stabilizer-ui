"""Recording the stream data of selected sources to HDF5 files."""

from __future__ import annotations

import datetime
import json
import logging
import math
import queue
import threading
import time

import h5py
import numpy as np
from stabilizer.stream import wrap
from stabilizer.stream_parser import Parser

from .decoders import source_scales, to_machine_units

logger = logging.getLogger(__name__)

FILE_FORMAT = "stabilizer-ui stream recording"
FILE_VERSION = 1

#: Interval at which the received data is written to the file, in seconds.
WRITE_INTERVAL = 0.5
#: Length of the chunks of the datasets, in samples.
CHUNK_LENGTH = 1 << 16
#: Longest gap in the stream data which is filled with zeros, in seconds. The recording
#: stops after longer interruptions (e.g. while another client has the stream), so that
#: each file is one continuous time series.
MAX_GAP = 10.0
#: Most data waiting to be written, in bytes. If writing the file falls further behind
#: the stream, the recording stops.
MAX_BACKLOG = 256 << 20

DESCRIPTION = (
    "Stream data of a Stabilizer at the full sample rate. `channels` holds the recorded "
    "sources (stream channels) in machine units (two's complement), sampled every "
    "`sample_period` seconds; multiply by their `scale` for their `unit` (the scale of "
    "the ADCs does not include the AFE gain, see `settings`). `lost` lists the ranges "
    "of samples lost in transmission as (first sample, number of samples); they are "
    "zero. `start_time` is the time of the computer when the first data arrived, and "
    "`settings` the settings of the device and the UI at the start (topic path to "
    "value, and the firmware version). The file is written in SWMR mode: open it with "
    "`swmr=True` (and call `refresh()` on the datasets) to read it while recording.")


def _signed(value: int) -> int:
    """A wrapped 32 bit difference as signed integer."""
    return value - (1 << 32) if value >= 1 << 31 else value


class StreamRecorder:
    """Writes the data of selected stream sources to an HDF5 file.

    The stream thread passes the frames it receives to `add()`, and a thread of the
    recorder writes them to the file. The other methods and the attributes are for the
    main thread.
    """

    def __init__(self,
                 path: str,
                 parser: Parser,
                 sources: list[int],
                 sample_period: float,
                 device: str = "",
                 settings: dict | None = None):
        """
        :param sources: Indices of the stream sources to record.
        :param settings: Snapshot of the settings to store.
        """
        self.path = path
        self.sample_period = sample_period
        self._parser = parser
        self._sources = sources
        #: Number of samples of each source recorded (including lost ones).
        self.samples = 0
        #: Number of samples lost in transmission (filled with zeros).
        self.lost = 0
        #: Why the recording has stopped by itself (if it has).
        self.error: str | None = None
        #: `time.monotonic()` when stream data was last added (`None` before any).
        self.last_data: float | None = None

        self._stopped = False
        self._start_time: datetime.datetime | None = None
        #: The frames to write (lists of them, see `add()`), and `None` to finish.
        self._queue = queue.SimpleQueue()
        #: Bytes of frame data added, and written (or left out) by the writer thread.
        self._added = 0
        self._processed = 0
        #: Sequence number of the next batch to write, once the first frame is written.
        self._next: int | None = None
        self.batch_size: int | None = None

        self._file = h5py.File(path, "w", libver=("v110", "latest"))
        try:
            self._create(device, settings)
        except BaseException:
            self._file.close()
            raise
        self._thread = threading.Thread(target=self._run, name="recorder")
        self._thread.start()

    def _create(self, device: str, settings: dict | None):
        f = self._file
        f.attrs["format"] = FILE_FORMAT
        f.attrs["version"] = FILE_VERSION
        f.attrs["description"] = DESCRIPTION
        f.attrs["device"] = device
        f.attrs["sample_period"] = self.sample_period
        f.attrs["settings"] = json.dumps(settings or {})
        names = self._parser.StreamData._fields
        scales = source_scales(self._parser)
        units = self._parser.units()
        channels = f.create_group("channels", track_order=True)
        self._datasets = []
        for i in self._sources:
            dataset = channels.create_dataset(names[i],
                                              shape=(0, ),
                                              maxshape=(None, ),
                                              dtype=np.int16,
                                              chunks=(CHUNK_LENGTH, ))
            dataset.attrs["scale"] = scales[i]
            dataset.attrs["unit"] = units[i]
            self._datasets.append(dataset)
        self._lost = f.create_dataset("lost",
                                      shape=(0, 2),
                                      maxshape=(None, 2),
                                      dtype=np.int64,
                                      chunks=(1024, 2))

    @property
    def names(self) -> list[str]:
        """Names of the recorded sources."""
        return [self._parser.StreamData._fields[i] for i in self._sources]

    @property
    def duration(self) -> float:
        """Duration of the data recorded, in seconds."""
        return self.samples * self.sample_period

    @property
    def size(self) -> int:
        """Size of the data recorded, in bytes."""
        return 2 * self.samples * len(self._sources)

    @property
    def finished(self) -> bool:
        """Whether the recording has ended, and the file is closed."""
        return not self._thread.is_alive()

    def add(self, frames: list[tuple[tuple, bytes]]):
        """Add the frames received since the previous call, as (header, body) (called
        from the stream thread)."""
        if self._stopped or not frames:
            return
        if self._start_time is None:
            self._start_time = datetime.datetime.now().astimezone()
        self.last_data = time.monotonic()
        size = sum(len(body) for _, body in frames)
        if self._added - self._processed + size > MAX_BACKLOG:
            self._fail("writing the file could not keep up with the stream")
            return
        self._added += size
        self._queue.put(frames)

    def stop(self):
        """End the recording: the data added so far is written, and the file closed (see
        `finished`)."""
        if not self._stopped:
            self._stopped = True
            self._queue.put(None)

    def wait(self, timeout: float | None = None):
        """Wait for the recording to end (see `stop()`)."""
        self._thread.join(timeout)

    def _fail(self, message: str):
        if self.error is None:
            self.error = message
            logger.warning("Recording to %s stopped: %s", self.path, message)
        self.stop()

    def _run(self):
        pending = []
        last_write = time.monotonic()
        try:
            while self.error is None:
                try:
                    frames = self._queue.get(timeout=WRITE_INTERVAL)
                except queue.Empty:
                    frames = []
                if frames is None:
                    break
                pending.extend(frames)
                now = time.monotonic()
                if pending and now - last_write >= WRITE_INTERVAL:
                    self._write(pending)
                    pending, last_write = [], now
                if self.last_data is not None and now - self.last_data > MAX_GAP:
                    self._fail(f"no stream data for more than {MAX_GAP:g} s")
            if pending:
                self._write(pending)
        except Exception as e:
            logger.exception("Failed to write %s", self.path)
            self._fail(f"failed to write the file: {e}")
        finally:
            self._stopped = True
            try:
                self._file.close()
            except Exception:
                logger.exception("Failed to close %s", self.path)

    def _start(self, header, body: bytes):
        """Write what is known once the first frame has arrived, and start SWMR mode
        (after which no attributes can be added)."""
        self.batch_size = len(body) // (2 * self._parser.n_sources * header.batches)
        self._next = header.sequence
        self._file.attrs["start_time"] = self._start_time.isoformat(
            timespec="microseconds")
        self._file.attrs["batch_size"] = self.batch_size
        self._file.swmr_mode = True

    def _write(self, frames: list[tuple[tuple, bytes]]):
        """Write frames in the order of their sequence numbers, leaving out data already
        written and filling gaps with zeros."""
        if self._next is None:
            self._start(*frames[0])
        n_sources, batch_size = self._parser.n_sources, self.batch_size
        max_gap = math.ceil(MAX_GAP / (batch_size * self.sample_period))
        error = None

        # Frames as (offset from the next batch, number of batches, body).
        placed = []
        for header, body in frames:
            if len(body) != 2 * n_sources * batch_size * header.batches:
                logger.warning("Frame of unexpected size %d, ignoring", len(body))
                continue
            offset = _signed(wrap(header.sequence - self._next))
            if offset + header.batches > 0:
                placed.append((offset, header.batches, body))
            elif offset < -max_gap:
                error = "the stream has restarted (the device seems to have)"
            # Otherwise already written (or filled), e.g. a duplicate or late frame.
        placed.sort(key=lambda frame: frame[0])
        end = 0
        for i, (offset, batches, _) in enumerate(placed):
            if offset - end > max_gap:
                error = f"the stream was interrupted for more than {MAX_GAP:g} s"
                del placed[i:]
                break
            end = max(end, offset + batches)

        if placed:
            raw = np.zeros((end, n_sources, batch_size), np.int16)
            received = np.zeros(end, bool)
            for offset, batches, body in placed:
                frame = np.frombuffer(body, "<i2").reshape(batches, n_sources, batch_size)
                start = max(offset, 0)
                raw[start:offset + batches] = frame[start - offset:]
                received[start:offset + batches] = True
            data = raw.transpose(1, 0, 2).reshape(n_sources, -1)
            to_machine_units(self._parser, data)
            data = data[self._sources]
            lost = ~received
            if lost.any():
                data.reshape(len(self._sources), end, batch_size)[:, lost] = 0
                edges = np.flatnonzero(np.diff(lost, prepend=False, append=False))
                starts, stops = edges[0::2], edges[1::2]
                ranges = np.stack(
                    [self.samples + starts * batch_size, (stops - starts) * batch_size],
                    axis=1)
                self._lost.resize((len(self._lost) + len(ranges), 2))
                self._lost[-len(ranges):] = ranges
                self._lost.flush()
                self.lost += int(ranges[:, 1].sum())
            n = self.samples + end * batch_size
            for dataset, values in zip(self._datasets, data):
                dataset.resize((n, ))
                dataset[self.samples:] = values
                dataset.flush()
            self.samples = n
            self._next = wrap(self._next + end)

        self._processed += sum(len(body) for _, body in frames)
        if error is not None:
            self._fail(error)
