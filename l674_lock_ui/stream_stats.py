from stabilizer.stream import AdcDac, wrap
import time
from dataclasses import dataclass


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