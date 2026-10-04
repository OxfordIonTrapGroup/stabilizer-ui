from collections.abc import Buffer

import numpy as np

#: FFT size of each stage.
FFT_SIZE: int


def sweep_excitation(sweep: tuple[int, int, int, float, float]) -> np.ndarray:
    """The excitation of the firmware `SweptSine` source for `sweep` (as for
    `analyse_sweep()`), in volts: exactly what the device adds to the DAC output, as it is
    reproduced with the firmware's own oscillator (`idsp`) and scaling.
    """


def analyse_sweep(runs: list[np.ndarray],
                  sweep: tuple[int, int, int, float, float],
                  reference: int,
                  batch_size: int,
                  ir_window: float,
                  points_per_decade: int = 100,
                  max_harmonic: int = 4,
                  offsets: list[int] | None = None,
                  gaps: list[np.ndarray] | None = None,
                  memory: int = 2 << 30) -> dict:
    """Transfer function estimation from an exponential sine sweep (`src/ess.rs`), as
    called by `stabilizer_ui.transfer_function.ess.analyse()` (see there for the
    parameters and the conversion of the result).

    `runs` are C-contiguous float64 arrays (channels, samples) in volts, with lost data
    interpolated linearly; `gaps` int64 arrays (n, 2) of the sample ranges of such data;
    `sweep` is `(rate, state, length, amplitude, sample_period)`. The channels of the
    runs are analysed in parallel, as many at a time as fit the `memory` budget (in
    bytes) for their buffers. Returns a dict of the results, with frequencies in
    cycles/sample and times in samples. Raises `ValueError` if the data cannot be
    analysed.
    """


class Stage:
    """Information about one stage of a `PsdCascade`."""
    #: Index of the first bin of this stage in the merged spectrum.
    start: int
    #: Whether the stage is included in the merged spectrum.
    included: bool
    #: Number of averaged periodograms (up to `max_count`).
    count: int
    #: Averaging limit, after which the average continues exponentially weighted.
    max_count: int
    #: Range of FFT bins taken from this stage.
    bins: tuple[int, int]
    #: Decimation of the input of this stage.
    decimation: int
    #: Bin width (relative to the sample rate).
    bin_width: float
    #: Samples buffered for the next segment (including the overlap).
    pending: int
    #: Number of input samples of this stage processed (counting the overlap once, and not
    #: considering the averaging limit).
    processed: int


class PsdCascade:
    """Online power spectral density estimate of a signal.

    The signal is decimated by 8 in each stage of a cascade, and the stages average the
    periodograms of segments of `FFT_SIZE` samples (with a Hann window and 50 % overlap).
    Their spectra are merged into one with roughly constant relative resolution, which
    extends to lower frequencies the longer the estimate runs.

    Each stage averages up to `max_averages` periodograms, and then continues with an
    exponentially weighted average with that time constant (in periodograms of the
    stage). `averages` limits the number in the first stage, and that of each further
    stage to an eighth of the previous one, which gives all stages the same time constant.

    Frequencies are relative to the sample rate (cycles per sample), and the power
    spectral density is one-sided, in units of the signal squared per unit of relative
    frequency (i.e. divide by the sample rate for an absolute value).

    The methods can be called from several threads; the GIL is released while
    processing.
    """

    def __init__(self,
                 *,
                 detrend: str = "mean",
                 max_averages: int = 1000,
                 averages: int = 2**32 - 1) -> None:
        ...

    def process(self, x: Buffer) -> None:
        """Process samples, given as a buffer of float32 (e.g. a NumPy array)."""

    def set_detrend(self, detrend: str) -> None:
        """Set the detrending method for each segment: none, midpoint, span or mean."""

    def set_averages(self, max_averages: int, averages: int = 2**32 - 1) -> None:
        """Set the averaging limits (see the class documentation)."""

    def psd(self,
            *,
            min_count: int = 1,
            keep_overlap: bool = False,
            keep_transition_band: bool = False) -> tuple[list[float], list[float], list[Stage]]:
        """Return the merged spectrum as `(frequencies, psd, stages)`.

        The bins of each frequency are taken from the stage with the highest resolution
        that has at least `min_count` averages. `stages` describes all stages, from the
        lowest frequencies to the highest (the first stage).
        """
