from collections.abc import Buffer

#: FFT size of each stage.
FFT_SIZE: int


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
