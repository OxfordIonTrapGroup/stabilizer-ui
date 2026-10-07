"""Decimating the stream data by powers of two (`stabilizer_dsp.Decimator`)."""
import numpy as np
import pytest
from stabilizer.stream_parser import AdcDecoder, Parser, PhaseOffsetDecoder

from stabilizer_ui.stream.decimation import Decimation
from stabilizer_ui.stream.decoders import DacDecoder, phase_periods

dsp = pytest.importorskip("stabilizer_dsp")


def decimate(depth: int, x: np.ndarray, chunk: int | None = None) -> np.ndarray:
    """Decimate the (channel, sample) array `x`, passed in chunks of `chunk` samples."""
    decimator = dsp.Decimator(depth, len(x))
    chunk = chunk or x.shape[1]
    y = [
        decimator.process(np.ascontiguousarray(x[:, i:i + chunk], np.float32))
        for i in range(0, x.shape[1], chunk)
    ]
    return np.concatenate(y + [decimator.finish()], axis=1)


@pytest.mark.parametrize("depth", [0, 1, 3, 8, 12])
def test_alignment(depth):
    """Output n is the input at sample n * ratio: a ramp passes unchanged (away from the
    ends, where the input is extended by constants)."""
    ratio = 1 << depth
    n = 50 * ratio + 3
    x = np.arange(n)[None] * (1e-3 / ratio)
    y = decimate(depth, x, 5000)
    assert y.shape == (1, -(-n // ratio))
    margin = -(-dsp.Decimator(depth, 1).half_width // ratio)
    inner = np.arange(margin, y.shape[1] - margin)
    np.testing.assert_allclose(y[0, inner], inner * 1e-3, atol=1e-6)


def test_ends():
    """The input is taken to be constant before the first sample and after the last."""
    y = decimate(6, np.full((1, 10_000), -7.0))
    np.testing.assert_allclose(y, -7, rtol=1e-6)


def test_chunks():
    """The output does not depend on how the input is split up."""
    x = np.random.default_rng(0).normal(0, 1000, (3, 100_000)).astype(np.float32)
    whole = decimate(7, x)
    for chunk in [1, 77, 4096, 33_333]:
        np.testing.assert_array_equal(decimate(7, x[:, :20_000], chunk),
                                      decimate(7, x[:, :20_000]))
    np.testing.assert_array_equal(decimate(7, x, 12_345), whole)
    # The channels are independent.
    np.testing.assert_array_equal(decimate(7, x[1:2]), whole[1:2])


@pytest.mark.parametrize("depth", [1, 4, 10])
def test_response(depth):
    """Flat in the passband, with what would alias into it suppressed."""
    ratio = 1 << depth
    n_out = 1000
    t = np.arange((n_out + 100) * ratio)
    for f in [0.05, 0.39]:  # In cycles per output sample.
        x = np.cos(2 * np.pi * f / ratio * t)[None]
        y = decimate(depth, x)[0, 50:n_out]
        expected = np.cos(2 * np.pi * f * np.arange(50, n_out))
        np.testing.assert_allclose(y, expected, atol=2e-5)
    for f in [0.61, 0.9, 1.3]:
        x = np.cos(2 * np.pi * f / ratio * t)[None]
        y = decimate(depth, x)[0, 50:n_out]
        assert np.max(np.abs(y)) < 1e-6


def test_invalid():
    with pytest.raises(ValueError):
        dsp.Decimator(dsp.MAX_DECIMATION_DEPTH + 1, 1)
    with pytest.raises(ValueError):
        dsp.Decimator(2, 0)
    decimator = dsp.Decimator(2, 2)
    with pytest.raises(ValueError):
        decimator.process(np.zeros((3, 10), np.float32))
    assert decimator.process(np.zeros((2, 0), np.float32)).shape == (2, 0)
    assert decimator.finish().shape == (2, 0)
    with pytest.raises(ValueError):
        Decimation(6, [None])


def test_phase_periods():
    assert phase_periods(Parser([AdcDecoder(), DacDecoder()])) == [None] * 4
    fnc = Parser([AdcDecoder(), PhaseOffsetDecoder()])
    assert phase_periods(fnc) == [None, None, 1 << 14, 1 << 14]


def test_phase():
    """Phases are filtered as phasors, so that they can wrap around."""
    period, ratio = 1 << 14, 64
    n = 400 * ratio
    # A frequency offset of about 0.37 turns per output sample, plus noise.
    t = np.arange(n)
    rng = np.random.default_rng(1)
    turns = 0.37 / ratio * t + 0.1 + rng.normal(0, 0.02, n)
    data = np.round(turns * period).astype(np.int64) % period
    decimation = Decimation(ratio, [period])
    y, _ = decimation.process(data[None].astype(np.int16))
    y = np.concatenate([y, decimation.finish()[0]], axis=1)[0]
    assert y.shape == (n // ratio, ) and y.dtype == np.float32
    assert np.all((y >= 0) & (y <= period))
    expected = (0.37 * np.arange(n // ratio) + 0.1) * period
    error = (y - expected + period / 2) % period - period / 2
    assert np.max(np.abs(error[10:-10])) < 0.01 * period


def test_lost():
    """Lost data is interpolated linearly, and the output near it reported."""
    ratio = 16
    rng = np.random.default_rng(2)
    data = np.round(1000 * np.sin(np.arange(4000) / 300) +
                    rng.normal(0, 10, 4000)).astype(np.int16)[None]
    lost = np.array([[100, 140], [160, 162], [3000, 3001]])
    damaged = data.copy()
    interpolated = data.astype(np.float64)
    for start, stop in lost:
        damaged[:, start:stop] = 12345
        left, right = interpolated[0, start - 1], interpolated[0, stop]
        interpolated[0, start - 1:stop + 1] = np.linspace(left, right, stop - start + 2)

    decimation = Decimation(ratio, [None])
    # In two parts, the second starting with lost data.
    y1, ranges1 = decimation.process(damaged[:, :3000], lost[:2])
    y2, ranges2 = decimation.process(damaged[:, 3000:], lost[2:] - 3000)
    y = np.concatenate([y1, y2, decimation.finish()[0]], axis=1)
    np.testing.assert_allclose(y, decimate(4, interpolated), atol=1e-3)
    # The output samples less than 16 input samples from lost data: 6 to 9 for 100 to 139,
    # 10 and 11 for 160 and 161 (merged), and 187 and 188 for 3000.
    np.testing.assert_array_equal(ranges1, [[6, 12]])
    np.testing.assert_array_equal(ranges2, [[187, 189]])


@pytest.mark.parametrize("depth", [1, 4, 9])
def test_extrapolated(depth):
    """Exactly the output samples whose filter reaches beyond the input are counted as
    extrapolated (`edge`, and the count from `finish()`); the others are those of the
    input as part of a longer signal."""
    ratio = 1 << depth
    rng = np.random.default_rng(3)
    edge = Decimation(ratio, [None]).edge
    half_width = Decimation(ratio, [None]).half_width
    start = (edge + 3) * ratio  # At an output sample of the longer signal.
    # The end at different phases of the output samples.
    for length in [100 * ratio, 100 * ratio + 1, 100 * ratio + ratio // 2 + 1]:
        signal = rng.normal(0, 1000, (1, start + length + (edge + 3) * ratio))
        whole = Decimation(ratio, [None])
        reference = np.concatenate([whole.process(signal)[0], whole.finish()[0]], axis=1)
        part = Decimation(ratio, [None])
        y = part.process(signal[:, start:start + length])[0]
        tail, extrapolated = part.finish()
        y = np.concatenate([y, tail], axis=1)
        n = np.arange(y.shape[1])
        assert edge == np.sum(n * ratio < half_width)
        assert extrapolated == np.sum(n * ratio + half_width >= length)
        affected = (n < edge) | (n >= len(n) - extrapolated)
        reference = reference[:, start // ratio:][:, :len(n)]
        np.testing.assert_array_equal(y[:, ~affected], reference[:, ~affected])
        assert y[0, 0] != reference[0, 0] and y[0, -1] != reference[0, -1]
