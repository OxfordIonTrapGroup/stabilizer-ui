//! Python bindings for the online power spectral density estimation of
//! [stabilizer-stream](https://github.com/quartiq/stabilizer-stream).

use std::sync::{Mutex, MutexGuard, PoisonError};

use pyo3::{buffer::PyBuffer, exceptions::PyValueError, prelude::*};
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

#[pymodule]
fn stabilizer_psd(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("FFT_SIZE", FFT_SIZE)?;
    m.add_class::<PsdCascade>()?;
    m.add_class::<Stage>()?;
    Ok(())
}
