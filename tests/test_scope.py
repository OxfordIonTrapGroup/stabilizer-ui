"""The scope's buffer of the stream data, and its spectrum."""
import numpy as np
import pytest
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD as TS

from stabilizer_ui.stream.fft_scope import SCOPE_TIME_SCALE, ScopeConfig
from stabilizer_ui.stream.thread import _ScopeBuffer

#: Standard deviation of the white noise used.
SIGMA = 1e-3
#: One-sided amplitude spectral density of that noise.
ASD = SIGMA * np.sqrt(2 * TS)


def noise(n_sources: int, n: int, seed=0) -> np.ndarray:
    return SIGMA * np.random.default_rng(seed).standard_normal(
        (n_sources, n)).astype(np.float32)


@pytest.mark.parametrize("chunk", [1, 7, 10, 25])
def test_scope_buffer(chunk):
    buffer = _ScopeBuffer(2, 10)
    data = np.arange(2 * 47, dtype=np.float32).reshape(2, 47)
    for i in range(0, 47, chunk):
        buffer.add(data[:, i:i + chunk])
    assert buffer.total == 47
    np.testing.assert_array_equal(buffer.latest(), data[:, -10:])
    buffer.resize(4)
    np.testing.assert_array_equal(buffer.latest(), data[:, -4:])
    buffer.resize(6)
    np.testing.assert_array_equal(buffer.latest()[:, 2:], data[:, -4:])
    np.testing.assert_array_equal(buffer.latest()[:, :2], 0)


def test_scope_spectrum():
    """The scope shows the amplitude spectral density, as an envelope on a log grid."""
    config = ScopeConfig(1 << 16, TS, fft=True)
    data = noise(16, config.length)
    frequencies, envelope = config.precondition(data)[0]
    assert np.all(np.diff(frequencies) >= 0)
    assert np.all(envelope[1::2] >= envelope[0::2])
    assert frequencies[0] == config.frequencies[1]
    assert frequencies[-1] <= 0.5 / TS * SCOPE_TIME_SCALE
    # Average the periodograms of all traces, without the envelope.
    window, scale = config._window
    spectra = np.abs(np.fft.rfft(data * window, axis=1)) * scale
    assert np.sqrt(np.mean(spectra[:, 1:-1]**2)) == pytest.approx(ASD, rel=0.01)
