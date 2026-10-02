"""The long-term spectral density estimate."""
import numpy as np
import pytest
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD as TS

# The extension (`psd/`) is optional.
pytest.importorskip("stabilizer_psd")

from stabilizer_ui.spectral_density.estimate import (  # noqa: E402
    Estimate, Spectrum, load_csv, save_csv)

#: Standard deviation of the white noise used.
SIGMA = 1e-3
#: One-sided amplitude spectral density of that noise.
ASD = SIGMA * np.sqrt(2 * TS)


def noise(n_sources: int, n: int, seed=0) -> np.ndarray:
    return SIGMA * np.random.default_rng(seed).standard_normal(
        (n_sources, n)).astype(np.float32)


def test_estimate():
    estimate = Estimate(["a", "b"], ["V", "V"], TS, max_averages=1000, detrend="mean")
    for i in range(16):
        estimate.process(noise(2, 1 << 16, seed=i), 0)
    assert estimate.duration == pytest.approx(16 * (1 << 16) * TS)
    spectra, stages = estimate.spectra(min_count=4, timestamp="")
    assert [s.name for s in spectra] == ["a", "b"]
    spectrum = spectra[0]
    assert spectrum.frequencies[0] > 0
    assert spectrum.frequencies[-1] == pytest.approx(0.5 / TS)
    assert np.median(spectrum.asd) == pytest.approx(ASD, rel=0.03)
    # The stages are ordered from low to high frequencies.
    included = [stage for stage in stages if stage.included]
    assert all(stage.count >= 4 for stage in included)
    assert included[-1].decimation == 1
    # The cumulative RMS of white noise above DC is its standard deviation.
    assert spectrum.cumulative_rms()[0] == pytest.approx(SIGMA, rel=0.03)


def test_cumulative_rms():
    f = np.linspace(1, 101, 1001)
    spectrum = Spectrum("a", "V", f, np.full_like(f, 2.0), 1.0, "")
    rms = spectrum.cumulative_rms()
    np.testing.assert_allclose(rms, 2 * np.sqrt(f[-1] - f))


def test_csv(tmp_path):
    spectra = [
        Spectrum("ADC0 12:00:00", "V", np.array([1.0, 2.0, 3.0]),
                 np.array([1e-6, 2e-6, 3e-6]), 10.0, "2026-10-02T12:00:00"),
        Spectrum("phase", "turns", np.array([0.5]), np.array([4e-3]), 5.0,
                 "2026-10-02T12:00:01"),
    ]
    path = tmp_path / "psd.csv"
    save_csv(path, spectra)
    loaded = load_csv(path)
    for a, b in zip(loaded, spectra):
        assert (a.name, a.unit, a.duration, a.timestamp) == (b.name, b.unit, b.duration,
                                                             b.timestamp)
        np.testing.assert_allclose(a.frequencies, b.frequencies, rtol=1e-6)
        np.testing.assert_allclose(a.asd, b.asd, rtol=1e-6)
    path.write_text("frequency,asd\n1,2\n")
    with pytest.raises(ValueError):
        load_csv(path)
