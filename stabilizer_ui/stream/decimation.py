"""Decimating the stream data by powers of two while recording it (`StreamRecorder`).

The filtering is done by `stabilizer_dsp.Decimator`, the half-band filter cascade of idsp.
"""

from __future__ import annotations

import numpy as np

try:
    from stabilizer_dsp import Decimator
except ImportError:  # The extension is optional (it needs a Rust toolchain to build).
    Decimator = None

#: Largest decimation offered, as power of two (to 0.75 Hz for `dual_iir`).
MAX_DEPTH = 20
#: Bandwidth of the decimated data (flat, and free of aliases), relative to its sample
#: rate (`idsp::hbf::HBF_PASSBAND`).
PASSBAND = 0.4


def available() -> bool:
    """Whether decimation is available, i.e. the `stabilizer_dsp` extension is."""
    return Decimator is not None


class Decimation:
    """Low-pass filters and decimates stream data by a power of two, as it comes in.

    Output sample `n` is the filtered data at input sample `n * ratio`, counting from the
    first input sample (before which the data is taken to be equal to it, as after the
    last). Phases are filtered as phasors (and come out in `[0, period]`), and lost data
    is interpolated linearly first.

    The filter of each output sample spans `half_width` input samples on either side, so
    the first `edge` output samples (and up to as many at the end, see `finish()`) are
    computed in part from the extended input.
    """

    def __init__(self, ratio: int, periods: list[float | None]):
        """
        :param ratio: The decimation, a power of two.
        :param periods: For each source, the period in machine units if it is a phase (see
            `decoders.phase_periods()`), or else `None`.
        """
        depth = ratio.bit_length() - 1
        if ratio < 1 or ratio != 1 << depth:
            raise ValueError(f"Decimation by {ratio}, which is not a power of two")
        if Decimator is None:
            raise RuntimeError("Decimation needs the stabilizer_dsp extension (the dsp "
                               "dependency group)")
        self.ratio = ratio
        self._periods = periods
        #: Number of channels filtered (two per phase).
        self._channel_count = sum(1 if p is None else 2 for p in periods)
        self._decimator = Decimator(depth, self._channel_count)
        #: Number of input samples of each source processed.
        self._received = 0
        #: The last input sample of each filtered channel.
        self._last: np.ndarray | None = None

    @property
    def half_width(self) -> int:
        """The number of input samples on either side of its own that the filter of an
        output sample spans (see `Decimator.half_width`)."""
        return self._decimator.half_width

    @property
    def edge(self) -> int:
        """The number of output samples whose filter reaches before the first input
        sample."""
        return -(-self.half_width // self.ratio)

    def process(self,
                data: np.ndarray,
                lost: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Decimate the next input samples.

        :param data: The data in machine units, as (source, sample) array.
        :param lost: The ranges of samples lost (whose values are ignored), as array of
            (first, stop) relative to `data`.
        :return: The output samples which are determined by now, as (source, sample)
            float32 array, and the ranges of output samples less than one output sample
            period away from lost data, as array of (first, stop) counting from the first
            output sample.
        """
        x = self._channels(data)
        if lost is None:
            lost = np.empty((0, 2), np.int64)
        for start, stop in lost:
            left = x[:, start - 1] if start > 0 else self._last
            right = x[:, stop] if stop < x.shape[1] else left
            if left is None:
                left = right
            if left is None:
                continue  # Nothing received at all.
            t = np.arange(1, stop - start + 1) / (stop - start + 1)
            x[:, start:stop] = left[:, None] + (right - left)[:, None] * t
        if x.shape[1]:
            self._last = x[:, -1].copy()
        y = self._sources(self._decimator.process(x))

        # The output samples less than `ratio` input samples from lost ones.
        first = (lost[:, 0] + self._received) // self.ratio
        stop = (lost[:, 1] + self._received - 2) // self.ratio + 2
        ranges = []
        for f, s in zip(first, stop):
            if ranges and f <= ranges[-1][1]:
                ranges[-1][1] = max(ranges[-1][1], s)
            else:
                ranges.append([f, s])
        self._received += data.shape[1]
        return y, np.array(ranges, np.int64).reshape(-1, 2)

    def finish(self) -> tuple[np.ndarray, int]:
        """The remaining output samples, up to the last one at or before the last input
        sample (as for `process()`), and the number of output samples at the end whose
        filter reaches after the last input sample (at most `edge`)."""
        data = self._sources(self._decimator.finish())
        # Output n reaches beyond the last input sample if n * ratio + half_width is at
        # least the number of input samples.
        total = -(-self._received // self.ratio)
        first = max(0, -(-(self._received - self.half_width) // self.ratio))
        return data, total - first

    def _channels(self, data: np.ndarray) -> np.ndarray:
        """The channels to filter: the sources, with phases as cosine and sine."""
        x = np.empty((self._channel_count, data.shape[1]), np.float32)
        i = 0
        for values, period in zip(data, self._periods):
            if period is None:
                x[i] = values
                i += 1
            else:
                phase = (2 * np.pi / period) * values
                x[i], x[i + 1] = np.cos(phase), np.sin(phase)
                i += 2
        return x

    def _sources(self, y: np.ndarray) -> np.ndarray:
        """The sources from the filtered channels."""
        data = np.empty((len(self._periods), y.shape[1]), np.float32)
        i = 0
        for j, period in enumerate(self._periods):
            if period is None:
                data[j] = y[i]
                i += 1
            else:
                phase = np.arctan2(y[i + 1], y[i], dtype=np.float64)
                data[j] = np.mod(phase * (period / (2 * np.pi)), period)
                i += 2
        return data
