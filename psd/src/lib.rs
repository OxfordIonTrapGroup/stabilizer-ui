//! Python bindings for the online power spectral density estimation of
//! [stabilizer-stream](https://github.com/quartiq/stabilizer-stream).

mod ess;

use std::sync::{Mutex, MutexGuard, PoisonError};

use numpy::{PyArray1, PyArray2, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::{buffer::PyBuffer, exceptions::PyValueError, prelude::*, types::PyDict};
use stabilizer_stream::{AvgOpts, Detrend, MergeOpts};

/// FFT size of each stage.
const FFT_SIZE: usize = 1 << 9;

fn parse_detrend(name: &str) -> PyResult<Detrend> {
    Ok(match name {
        "none" => Detrend::None,
        "midpoint" => Detrend::Midpoint,
        "span" => Detrend::Span,
        "mean" => Detrend::Mean,
        _ => {
            return Err(PyValueError::new_err(format!(
                "unknown detrend method {name:?} (expected none, midpoint, span or mean)"
            )))
        }
    })
}

fn avg_opts(averages: u32, max_averages: u32) -> AvgOpts {
    // A stage averages one more spectrum than the given number (as in the `psd` app).
    AvgOpts {
        limit: max_averages.saturating_sub(1),
        count: averages.saturating_sub(1),
    }
}

/// Online power spectral density estimate of a signal.
///
/// The signal is decimated by 8 in each stage of a cascade, and the stages average the
/// periodograms of segments of `FFT_SIZE` samples (with a Hann window and 50 % overlap).
/// Their spectra are merged into one with roughly constant relative resolution, which
/// extends to lower frequencies the longer the estimate runs.
///
/// Each stage averages up to `max_averages` periodograms, and then continues with an
/// exponentially weighted average with that time constant (in periodograms of the
/// stage). `averages` limits the number in the first stage, and that of each further
/// stage to an eighth of the previous one, which gives all stages the same time constant.
///
/// Frequencies are relative to the sample rate (cycles per sample), and the power
/// spectral density is one-sided, in units of the signal squared per unit of relative
/// frequency (i.e. divide by the sample rate for an absolute value).
///
/// The methods can be called from several threads; the GIL is released while
/// processing.
#[pyclass(frozen)]
struct PsdCascade {
    inner: Mutex<stabilizer_stream::PsdCascade<FFT_SIZE>>,
}

impl PsdCascade {
    fn lock(&self) -> MutexGuard<'_, stabilizer_stream::PsdCascade<FFT_SIZE>> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

#[pymethods]
impl PsdCascade {
    #[new]
    #[pyo3(signature = (*, detrend = "mean", max_averages = 1000, averages = u32::MAX))]
    fn new(detrend: &str, max_averages: u32, averages: u32) -> PyResult<Self> {
        let mut inner = stabilizer_stream::PsdCascade::default();
        inner.set_detrend(parse_detrend(detrend)?);
        inner.set_avg(avg_opts(averages, max_averages));
        Ok(Self {
            inner: Mutex::new(inner),
        })
    }

    /// Process samples, given as a buffer of float32 (e.g. a NumPy array).
    fn process(&self, py: Python<'_>, x: PyBuffer<f32>) -> PyResult<()> {
        let x = x.to_vec(py)?;
        py.detach(|| self.lock().process(&x));
        Ok(())
    }

    /// Set the detrending method for each segment: none, midpoint, span or mean.
    fn set_detrend(&self, py: Python<'_>, detrend: &str) -> PyResult<()> {
        let detrend = parse_detrend(detrend)?;
        py.detach(|| self.lock().set_detrend(detrend));
        Ok(())
    }

    /// Set the averaging limits (see the class documentation).
    #[pyo3(signature = (max_averages, averages = u32::MAX))]
    fn set_averages(&self, py: Python<'_>, max_averages: u32, averages: u32) {
        py.detach(|| self.lock().set_avg(avg_opts(averages, max_averages)));
    }

    /// Return the merged spectrum as `(frequencies, psd, stages)`.
    ///
    /// The bins of each frequency are taken from the stage with the highest resolution
    /// that has at least `min_count` averages. `stages` describes all stages, from the
    /// lowest frequencies to the highest (the first stage).
    #[pyo3(signature = (*, min_count = 1, keep_overlap = false, keep_transition_band = false))]
    fn psd(
        &self,
        py: Python<'_>,
        min_count: u32,
        keep_overlap: bool,
        keep_transition_band: bool,
    ) -> (Vec<f32>, Vec<f32>, Vec<Stage>) {
        let opts = MergeOpts {
            keep_overlap,
            min_count,
            keep_transition_band,
        };
        py.detach(|| {
            let (psd, breaks) = self.lock().psd(&opts);
            let frequencies = stabilizer_stream::Break::frequencies(&breaks);
            let stages = breaks
                .iter()
                .map(|b| Stage {
                    start: b.start,
                    included: b.include,
                    count: b.count,
                    max_count: b.avg.saturating_add(1),
                    bins: (b.bins.start, b.bins.end),
                    decimation: b.decimation,
                    bin_width: b.rbw(),
                    pending: b.pending,
                    processed: b.processed,
                })
                .collect();
            (frequencies, psd, stages)
        })
    }
}

/// Information about one stage of a `PsdCascade`.
#[pyclass(frozen, get_all)]
struct Stage {
    /// Index of the first bin of this stage in the merged spectrum.
    start: usize,
    /// Whether the stage is included in the merged spectrum.
    included: bool,
    /// Number of averaged periodograms (up to `max_count`).
    count: u32,
    /// Averaging limit, after which the average continues exponentially weighted.
    max_count: u32,
    /// Range of FFT bins taken from this stage.
    bins: (usize, usize),
    /// Decimation of the input of this stage.
    decimation: usize,
    /// Bin width (relative to the sample rate).
    bin_width: f32,
    /// Samples buffered for the next segment (including the overlap).
    pending: usize,
    /// Number of input samples of this stage processed (counting the overlap once, and
    /// not considering the averaging limit).
    processed: usize,
}

/// The sweep parameters `(rate, state, length, amplitude, sample_period)`.
fn parse_sweep(
    (rate, state, length, amplitude, sample_period): (i64, i64, usize, f64, f64),
) -> PyResult<ess::Sweep> {
    let rate = i32::try_from(rate).map_err(|_| PyValueError::new_err("Sweep rate out of range"))?;
    Ok(ess::Sweep::new(
        rate,
        state,
        length,
        amplitude,
        sample_period,
    ))
}

/// The excitation of the firmware `SweptSine` source for `sweep` (as for
/// `analyse_sweep()`), in volts: exactly what the device adds to the DAC output, as it is
/// reproduced with the firmware's own oscillator (`idsp`) and scaling, at the scale of
/// the stream data (`stabilizer.DAC_VOLTS_PER_LSB`).
#[pyfunction]
fn sweep_excitation<'py>(
    py: Python<'py>,
    sweep: (i64, i64, usize, f64, f64),
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let sweep = parse_sweep(sweep)?;
    Ok(PyArray1::from_vec(py, py.detach(|| sweep.excitation())))
}

/// Transfer function estimation from an exponential sine sweep (`ess.rs`), as called by
/// `stabilizer_ui.transfer_function.ess.analyse()` (see there for the parameters).
///
/// `runs` are C-contiguous float64 arrays (channels, samples) in volts, with lost data
/// interpolated linearly; `gaps` int64 arrays (n, 2) of the sample ranges of such data;
/// `sweep` is `(rate, state, length, amplitude, sample_period)`. The channels of the
/// runs are analysed in parallel, as many at a time as fit the `memory` budget (in
/// bytes) for their buffers. Returns a dict of the results, with frequencies in
/// cycles/sample and times in samples.
#[pyfunction]
#[pyo3(signature = (runs, sweep, reference, batch_size, ir_window, points_per_decade = 100, max_harmonic = 4, offsets = None, gaps = None, memory = 2 << 30))]
#[allow(clippy::too_many_arguments)]
fn analyse_sweep<'py>(
    py: Python<'py>,
    runs: Vec<PyReadonlyArray2<'py, f64>>,
    sweep: (i64, i64, usize, f64, f64),
    reference: usize,
    batch_size: usize,
    ir_window: f64,
    points_per_decade: u32,
    max_harmonic: u32,
    offsets: Option<Vec<i64>>,
    gaps: Option<Vec<PyReadonlyArray2<'py, i64>>>,
    memory: usize,
) -> PyResult<Bound<'py, PyDict>> {
    // The channels as slices of the arrays (no copy).
    let runs: Vec<Vec<&[f64]>> = runs
        .iter()
        .map(|run| {
            let length = run.shape()[1].max(1);
            Ok(run.as_slice()?.chunks_exact(length).collect())
        })
        .collect::<PyResult<_>>()?;
    let gaps: Option<Vec<Vec<(i64, i64)>>> = gaps.map(|gaps| {
        gaps.iter()
            .map(|g| {
                g.as_array()
                    .outer_iter()
                    .map(|row| (row[0], row[1]))
                    .collect()
            })
            .collect()
    });
    let sweep = parse_sweep(sweep)?;
    let settings = ess::Settings {
        ir_window,
        points_per_decade,
        max_harmonic,
        memory,
    };
    let analysis = py
        .detach(|| {
            ess::analyse(
                &runs,
                &sweep,
                reference,
                batch_size,
                &settings,
                offsets.as_deref(),
                gaps.as_deref(),
            )
        })
        .map_err(PyValueError::new_err)?;

    let array2 = |v: &[Vec<f64>]| PyArray2::from_vec2(py, v);
    let carray2 = |v: &[Vec<num_complex::Complex64>]| PyArray2::from_vec2(py, v);
    let dict = PyDict::new(py);
    dict.set_item("frequencies", PyArray1::from_vec(py, analysis.frequencies))?;
    dict.set_item("responses", carray2(&analysis.responses)?)?;
    dict.set_item("noise", array2(&analysis.noise)?)?;
    dict.set_item("harmonic_orders", analysis.harmonic_orders)?;
    dict.set_item(
        "harmonic_frequencies",
        analysis
            .harmonic_frequencies
            .into_iter()
            .map(|f| PyArray1::from_vec(py, f))
            .collect::<Vec<_>>(),
    )?;
    dict.set_item(
        "harmonics",
        analysis
            .harmonics
            .iter()
            .map(|h| carray2(h))
            .collect::<Result<Vec<_>, _>>()?,
    )?;
    dict.set_item(
        "harmonic_noise",
        analysis
            .harmonic_noise
            .iter()
            .map(|h| array2(h))
            .collect::<Result<Vec<_>, _>>()?,
    )?;
    dict.set_item("ir_start", analysis.ir_start)?;
    dict.set_item("impulse_responses", array2(&analysis.impulse_responses)?)?;
    dict.set_item("ir_window", analysis.ir_window)?;
    dict.set_item("noise_window", analysis.noise_window)?;
    dict.set_item("harmonic_windows", analysis.harmonic_windows)?;
    dict.set_item("offsets", analysis.offsets)?;
    dict.set_item("gap_frequencies", analysis.gap_frequencies)?;
    dict.set_item("warnings", analysis.warnings)?;
    Ok(dict)
}

#[pymodule]
fn stabilizer_psd(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("FFT_SIZE", FFT_SIZE)?;
    m.add_class::<PsdCascade>()?;
    m.add_class::<Stage>()?;
    m.add_function(wrap_pyfunction!(sweep_excitation, m)?)?;
    m.add_function(wrap_pyfunction!(analyse_sweep, m)?)?;
    Ok(())
}
