import json

import numpy as np
import pytest
from scipy import signal

import stabilizer.iir_coefficients as iir
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD as TS

from stabilizer_ui.transfer_function import ess

BATCH_SIZE = 8
#: Channel order of the dual-iir stream.
CHANNELS = ["ADC0", "ADC1", "DAC0", "DAC1"]
#: Latency from DAC to ADC in the stream, in samples.
LATENCY = 2 * BATCH_SIZE


def firmware_phase(sweep: ess.Sweep, n: int) -> np.ndarray:
    """Phase (in turns) of the first `n` samples of the firmware `SweptSine` source,
    following the integer arithmetic of `idsp::Sweep` and `idsp::AccuOsc`."""
    state, accu = sweep.state, 0
    phase = np.empty(n)
    for i in range(n):
        phase[i] = accu / 2**64
        frequency = state
        state += sweep.rate * ((frequency + (1 << 31)) >> 32)
        accu = (accu + frequency) % 2**64
    return phase


def _resonant_lowpass(f0, q):
    """Second-order low-pass filter (b, a) with the given resonance frequency and Q."""
    w0 = 2 * np.pi * f0
    return signal.bilinear([w0**2], [1, w0 / q, w0**2], fs=1 / TS)


def delayed(b, a, delay=LATENCY):
    return np.concatenate([np.zeros(delay), b]), a


def polysub(a, b):
    """Difference of polynomials in z^-1 (in ascending order)."""
    n = max(len(a), len(b))
    return np.pad(a, (0, n - len(a))) - np.pad(b, (0, n - len(b)))


def frequency_response(b, a, frequencies):
    return signal.freqz(b, a, worN=frequencies, fs=1 / TS)[1]


def make_runs(sweep,
              adc_from_s,
              dac_from_s,
              offset=1024,
              ir_window=None,
              noise=0.0,
              nonlinearity=None,
              seed=0,
              n_runs=1):
    """Simulate captured data of a dual-iir measurement exciting channel 0.

    `adc_from_s`/`dac_from_s` are (b, a) of the transfer functions from the stimulus to
    ADC0/DAC0. A `nonlinearity` is applied to the stimulus (in volts) before
    `adc_from_s`.
    """
    rng = np.random.default_rng(seed)
    ir_window = ir_window or ess.default_ir_window(sweep)
    tail = int(ess.capture_tail(ir_window) / TS)
    runs = []
    for run in range(n_runs):
        start = offset + BATCH_SIZE * run
        length = start + sweep.length + tail
        s = np.zeros(length)
        s[start:start + sweep.length] = sweep.excitation()
        u = s if nonlinearity is None else nonlinearity(s)
        adc0 = signal.lfilter(*adc_from_s, u) + noise * rng.standard_normal(length)
        dac0 = signal.lfilter(*dac_from_s, s) + 0.3
        adc1 = 1e-3 * adc0 + 0.1
        dac1 = np.full(length, -0.2)
        runs.append(np.array([adc0, adc1, dac0, dac1]))
    return runs


def analyse(sweep, runs, ir_window=None, **kwargs):
    settings = ess.AnalysisSettings(ir_window or ess.default_ir_window(sweep), **kwargs)
    return ess.analyse(runs, sweep, CHANNELS.index("DAC0"), BATCH_SIZE, settings)


def by_name(values):
    return dict(zip(CHANNELS + [ess.filter_output_name(0)], values))


def test_design():
    sweep = ess.Sweep.design(100, 300e3, 0.5, 1.0, TS)
    assert sweep.cycles == round(sweep.cycles) >= 1
    assert sweep.f_start == pytest.approx(100, rel=0.1)
    assert sweep.f_stop == pytest.approx(300e3, rel=1e-4)
    assert sweep.duration == pytest.approx(0.5, rel=0.1)
    config = sweep.source_config()
    assert all(isinstance(config[key], int) for key in ["length", "state", "rate"])
    assert config["state"] < 2**63 and config["rate"] < 2**31
    json.dumps(config)

    # Short sweeps cannot start at low frequencies (at least one cycle per e-folding).
    short = ess.Sweep.design(1, 300e3, 0.1, 1.0, TS)
    assert short.cycles == 1
    assert short.f_start > 100

    with pytest.raises(ValueError):
        ess.Sweep.design(100, 0.5 / TS, 1, 1, TS)
    with pytest.raises(ValueError):
        ess.Sweep.design(100, 1e3, 1, 11, TS)
    with pytest.raises(ValueError):
        ess.Sweep.design(1e3, 100, 1, 1, TS)


def test_phase_matches_firmware():
    sweep = ess.Sweep.design(500, 300e3, 0.15, 1.0, TS)
    n = sweep.length
    phase = sweep.phase()
    error = phase - firmware_phase(sweep, n)
    error -= np.round(error)
    assert np.max(np.abs(error)) < 1e-9

    approximation = sweep.cycles * np.expm1(np.arange(n) * sweep.growth)
    error = phase - approximation
    error -= np.round(error)
    assert np.max(np.abs(error)) < 1e-3


def test_harmonic_delay():
    sweep = ess.Sweep.design(500, 300e3, 0.15, 1.0, TS)
    n = np.arange(1000)
    phase = sweep.cycles * np.expm1(n * sweep.growth)
    for k in [2, 3]:
        delay = sweep.harmonic_delay(k)
        shifted = sweep.cycles * np.expm1((n + delay) * sweep.growth)
        difference = shifted - k * phase
        assert np.allclose(difference - np.round(difference), 0, atol=1e-9)


def test_open_loop_plant():
    sweep = ess.Sweep.design(100, 300e3, 0.3, 0.5, TS)
    plant = delayed(*_resonant_lowpass(20e3, 5))
    runs = make_runs(sweep, plant, ([1], [1]))
    analysis = analyse(sweep, runs)

    assert analysis.offsets == [1024]
    assert not analysis.warnings
    f = analysis.frequencies
    assert f[0] == pytest.approx(sweep.f_start * 2**ess.BAND_EDGE_TAPER)
    responses = by_name(analysis.responses)
    expected = frequency_response(*plant, f)
    assert np.max(np.abs(responses["ADC0"] - expected) / np.abs(expected)) < 5e-3
    assert np.max(np.abs(responses["DAC0"] - 1)) < 1e-6
    assert np.max(np.abs(responses["ADC1"] / (1e-3 * expected) - 1)) < 5e-3
    assert np.max(np.abs(responses["DAC1"])) < 1e-6
    assert np.max(np.abs(responses["Y0"])) < 1e-6

    noise = by_name(analysis.noise)
    plant_estimate, _ = ess.derived_quantity("plant", responses, noise, 0)
    assert np.max(np.abs(plant_estimate - expected) / np.abs(expected)) < 5e-3


def test_closed_loop():
    sweep = ess.Sweep.design(50, 300e3, 0.5, 0.2, TS)

    # Plant: first-order low-pass at 5 kHz, gain 0.5, plus the converter latency.
    bp, ap = delayed(
        *signal.bilinear([0.5 * 2 * np.pi * 5e3], [1, 2 * np.pi * 5e3], fs=1 / TS))

    # PI controller (negative feedback for the positive plant gain).
    class Args:
        sample_period = TS
        Kp, Ki, Kii, Kd, Kdd = -0.2, -2e3, 0, 0, 0
        Ki_limit = Kii_limit = Kd_limit = Kdd_limit = np.inf

    ba = iir.pid_coefficients(Args)
    # idsp convention: y0 = b0 x0 + b1 x1 + b2 x2 + a1 y1 + a2 y2.
    bc, ac = np.array(ba[:3]), np.array([1, -ba[3], -ba[4]])

    # DAC = C ADC + s, ADC = P DAC.
    denominator = polysub(np.convolve(ac, ap), np.convolve(bc, bp))
    assert np.all(np.abs(np.roots(denominator)) < 1)
    dac_from_s = np.convolve(ac, ap), denominator
    adc_from_s = np.convolve(ac, bp), denominator

    runs = make_runs(sweep, adc_from_s, dac_from_s)
    analysis = analyse(sweep, runs, points_per_decade=50)
    assert analysis.offsets == [1024]

    f = analysis.frequencies
    responses = by_name(analysis.responses)
    noise = by_name(analysis.noise)
    plant = frequency_response(bp, ap, f)
    controller = frequency_response(bc, ac, f)

    def check(quantity, expected):
        value, _ = ess.derived_quantity(quantity, responses, noise, 0)
        error = np.abs(value - expected) / np.abs(expected)
        # The frequency resolution limits the accuracy near the start frequency.
        assert np.max(error[f > 3 * sweep.f_start]) < 1e-2, quantity
        assert np.max(error) < 5e-2, quantity

    check("plant", plant)
    check("controller", controller)
    check("loop", -controller * plant)
    check("sensitivity", 1 / (1 - controller * plant))


def harmonics_by_name(analysis, k):
    return by_name(analysis.harmonics[k]), by_name(analysis.harmonic_noise[k])


def test_harmonic_distortion():
    sweep = ess.Sweep.design(1e3, 100e3, 0.2, 1.0, TS)
    a2, a3, a4 = 0.01, 0.02, 0.008
    runs = make_runs(sweep,
                     delayed([1], [1]), ([1], [1]),
                     nonlinearity=lambda x: x + a2 * x**2 + a3 * x**3 + a4 * x**4)
    analysis = analyse(sweep, runs)

    # For the unit amplitude sweep, x^2 = (1 - cos 2φ) / 2, x^3 = (3 sin φ - sin 3φ) / 4,
    # and x^4 = (3 - 4 cos 2φ + cos 4φ) / 8.
    linear = 1 + 3 * a3 / 4
    expected = {2: (a2 + a4) / 2, 3: a3 / 4, 4: a4 / 8}
    assert np.allclose(np.abs(analysis.responses[0]), linear, rtol=1e-3)
    assert set(analysis.harmonics) == {2, 3, 4}
    for k, value in expected.items():
        assert np.allclose(np.abs(analysis.harmonics[k][0]), value, rtol=2e-2), k
        assert np.max(analysis.harmonic_frequencies[k]) <= sweep.f_stop / k

        # With the channel held, the DAC is the stimulus, so the plant harmonics are
        # those of the ADC.
        harmonics, harmonic_noise = harmonics_by_name(analysis, k)
        value, _ = ess.derived_harmonic("plant", harmonics, harmonic_noise,
                                        by_name(analysis.responses), analysis.frequencies,
                                        analysis.harmonic_frequencies[k], 0)
        assert np.allclose(value, harmonics["ADC0"], rtol=1e-6)
        assert np.all(np.isfinite(harmonic_noise["ADC0"]))


def test_harmonic_noise_estimate():
    # For a linear system, the harmonic responses are just noise.
    sweep = ess.Sweep.design(500, 300e3, 0.3, 0.1, TS)
    plant = delayed(*_resonant_lowpass(50e3, 2))
    runs = make_runs(sweep, plant, ([1], [1]), noise=3e-3)
    analysis = analyse(sweep, runs)
    for k in analysis.harmonics:
        harmonic, noise = analysis.harmonics[k][0], analysis.harmonic_noise[k][0]
        ratio = np.sqrt(np.mean(np.abs(harmonic)**2) / np.mean(noise**2))
        assert 0.5 < ratio < 2, k


def test_noise_estimate():
    sweep = ess.Sweep.design(200, 300e3, 0.2, 0.1, TS)
    plant = delayed(*_resonant_lowpass(50e3, 2))
    runs = make_runs(sweep, plant, ([1], [1]), noise=3e-3)
    analysis = analyse(sweep, runs)

    expected = frequency_response(*plant, analysis.frequencies)
    error = np.abs(analysis.responses[0] - expected)
    estimate = analysis.noise[0]
    assert np.all(np.isfinite(estimate))
    # Compare the RMS over frequency ranges (the error at each point is random).
    for chunk_error, chunk_estimate in zip(np.array_split(error, 5),
                                           np.array_split(estimate, 5)):
        ratio = np.sqrt(np.mean(chunk_error**2) / np.mean(chunk_estimate**2))
        assert 0.4 < ratio < 2.5


def test_noise_estimate_band_edge():
    # Near the band edges, the response window cuts the long ringing of the band
    # limiting filter, which the noise estimate must not mistake for noise. For a channel
    # with noise only, the bulk delay found is arbitrary, which makes this worse.
    sweep = ess.Sweep.design(200, 300e3, 0.2, 0.1, TS)
    plant = delayed(*_resonant_lowpass(50e3, 2))
    rng = np.random.default_rng(1)
    responses, estimates = [], []
    for run in make_runs(sweep, plant, ([1], [1]), noise=3e-3, n_runs=8):
        run[1] = 3e-3 * rng.standard_normal(run.shape[1])
        analysis = analyse(sweep, [run])
        responses.append(analysis.responses[1])
        estimates.append(analysis.noise[1])
    responses, estimates = np.array(responses), np.array(estimates)
    scatter = np.std(responses, axis=0, ddof=1)
    for points in [slice(0, 4), slice(4, 12), slice(-4, None)]:
        ratio = np.sqrt(np.mean(estimates[:, points]**2) / np.mean(scatter[points]**2))
        assert 0.5 < ratio < 1.8, points


def test_averaging():
    sweep = ess.Sweep.design(200, 300e3, 0.1, 0.1, TS)
    plant = delayed(*_resonant_lowpass(50e3, 2))
    single = analyse(sweep, make_runs(sweep, plant, ([1], [1]), noise=3e-3))
    averaged = analyse(sweep, make_runs(sweep, plant, ([1], [1]), noise=3e-3, n_runs=4))
    assert averaged.offsets == [1024, 1032, 1040, 1048]
    ratio = np.median(averaged.noise[0] / single.noise[0])
    assert ratio == pytest.approx(0.5, rel=0.2)


def test_missing_sweep():
    sweep = ess.Sweep.design(200, 300e3, 0.1, 0.1, TS)
    runs = make_runs(sweep, ([1], [1]), ([1], [1]))
    runs[0][2] = 0.3
    with pytest.raises(ess.AnalysisError):
        analyse(sweep, runs)
