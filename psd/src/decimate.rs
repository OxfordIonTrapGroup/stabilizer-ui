//! Decimation by powers of two with the half-band filter cascade of `idsp`
//! (`idsp::hbf::HBF_TAPS`, as in the PSD stages of stabilizer-stream), in real time.
//!
//! Each stage halves the rate. The stage at the lowest rate has the sharpest filter
//! (`HBF_TAPS.0`), the ones before it successively wider transition bands, so that the
//! cascade is flat up to `HBF_PASSBAND` (0.4) of the output rate and suppresses everything
//! which would alias into that band, at little cost: all stages together take about twice
//! the computation of the first.

use dsp_process::SplitProcess;
use idsp::hbf::{EvenSymmetric, HbfDec, HBF_TAPS};

/// Largest decimation, as a power of two.
pub const MAX_DEPTH: u32 = 24;

/// Output samples a stage computes at a time (its state holds this many beyond its taps).
const BLOCK: usize = 1 << 7;

/// Samples of the constant extension of the input fed at a time.
const EXTENSION_BLOCK: usize = 1 << 14;

/// A decimation by two.
trait HalfBand: Send {
    /// Decimate `x`, the input following that of the previous calls, appending the output
    /// to `y`. An odd sample at the end is kept for the next call.
    fn process(&mut self, x: &[f32], y: &mut Vec<f32>);

    /// The delay in output samples: output `j` is the filtered input at sample
    /// `2 (j - delay)`.
    fn delay(&self) -> usize;

    /// The number of input samples on either side of the centre the filter spans.
    fn half_width(&self) -> usize;

    /// Number of input samples which determine the state (even).
    fn memory(&self) -> usize;
}

struct Stage<const M: usize, const N: usize> {
    taps: &'static EvenSymmetric<[f32; M]>,
    state: HbfDec<[f32; N]>,
    /// The first sample of a pair, if the input so far has had an odd length.
    pending: Option<f32>,
}

impl<const M: usize, const N: usize> HalfBand for Stage<M, N> {
    fn process(&mut self, mut x: &[f32], y: &mut Vec<f32>) {
        if let Some(first) = self.pending.take() {
            let Some((&second, rest)) = x.split_first() else {
                self.pending = Some(first);
                return;
            };
            y.push(self.taps.process(&mut self.state, [first, second]));
            x = rest;
        }
        let (pairs, rest) = x.as_chunks::<2>();
        let start = y.len();
        y.resize(start + pairs.len(), 0.0);
        self.taps.block(&mut self.state, pairs, &mut y[start..]);
        self.pending = rest.first().copied();
    }

    fn delay(&self) -> usize {
        // The output for the pair (2j, 2j + 1) has the even sample of pair j - (M - 1) as
        // its centre tap.
        M - 1
    }

    fn half_width(&self) -> usize {
        // The response is 4M - 1 samples long, and symmetric.
        2 * M - 1
    }

    fn memory(&self) -> usize {
        4 * M
    }
}

macro_rules! stage {
    ($taps:expr) => {
        Box::new(
            Stage::<{ $taps.0.len() }, { 2 * $taps.0.len() - 1 + BLOCK }> {
                taps: &$taps,
                state: Default::default(),
                pending: None,
            },
        )
    };
}

/// The stage `steps` stages before the last one of a cascade.
fn stage(steps: u32) -> Box<dyn HalfBand> {
    match steps {
        0 => stage!(HBF_TAPS.0),
        1 => stage!(HBF_TAPS.1),
        2 => stage!(HBF_TAPS.2),
        3 => stage!(HBF_TAPS.3),
        // The transition bands of the stages further from the output are wider still.
        _ => stage!(HBF_TAPS.4),
    }
}

/// Feed `value` to the stages until it determines their states, as if the input had always
/// had that value.
fn prime(stages: &mut [Box<dyn HalfBand>], mut value: f32) {
    let mut y = Vec::new();
    for stage in stages {
        y.clear();
        stage.process(&vec![value; stage.memory()], &mut y);
        value = y[y.len() - 1];
    }
}

/// Pass `x` through `stages`, appending the output of the last one to `y`.
fn cascade(
    stages: &mut [Box<dyn HalfBand>],
    x: &[f32],
    y: &mut Vec<f32>,
    scratch: &mut [Vec<f32>; 2],
) {
    let Some((last, stages)) = stages.split_last_mut() else {
        y.extend_from_slice(x);
        return;
    };
    let mut input = x;
    let [a, b] = scratch;
    let (mut output, mut spare) = (a, b);
    for stage in stages {
        output.clear();
        stage.process(input, output);
        std::mem::swap(&mut output, &mut spare);
        input = spare;
    }
    last.process(input, y);
}

/// Decimation of several channels by `2^depth`.
///
/// Output `n` of each channel is the filtered input at sample `n 2^depth`, where the input is
/// taken to be equal to its first sample before it, and to its last sample after it
/// (`finish()`).
pub struct Decimator {
    /// The stages of each channel, from the input.
    channels: Vec<Vec<Box<dyn HalfBand>>>,
    depth: u32,
    /// The delay of the cascade in input samples: its output `j` is its input at sample
    /// `j ratio() - delay`.
    delay: usize,
    /// See `half_width()`.
    half_width: usize,
    /// Outputs of the cascade still to drop, as they are before the first input sample.
    skip: usize,
    /// Number of input samples of each channel.
    received: usize,
    /// Number of output samples of each channel.
    emitted: usize,
    /// The last input sample of each channel.
    last: Vec<f32>,
    finished: bool,
    scratch: [Vec<f32>; 2],
}

impl Decimator {
    pub fn new(depth: u32, channels: usize) -> Self {
        assert!(depth <= MAX_DEPTH);
        let stages = || (0..depth).rev().map(stage).collect::<Vec<_>>();
        let channels: Vec<_> = (0..channels).map(|_| stages()).collect();
        // Output `j` of stage `i` is the input of the cascade at sample
        // `2^(i + 1) j - sum_(l <= i) 2^(l + 1) delay_l`.
        let delay = channels.first().map_or(0, |stages| {
            stages
                .iter()
                .enumerate()
                .map(|(i, stage)| stage.delay() << (i + 1))
                .sum()
        });
        // That of stage `i` spans `2^i` times as many input samples of the cascade.
        let half_width = channels.first().map_or(0, |stages| {
            stages
                .iter()
                .enumerate()
                .map(|(i, stage)| stage.half_width() << i)
                .sum()
        });
        Self {
            last: vec![0.0; channels.len()],
            channels,
            depth,
            delay,
            half_width,
            skip: delay.div_ceil(1 << depth),
            received: 0,
            emitted: 0,
            finished: false,
            scratch: Default::default(),
        }
    }

    pub fn ratio(&self) -> usize {
        1 << self.depth
    }

    /// The number of input samples on either side of sample `n ratio()` that the filter of
    /// output `n` spans; it follows once the input up to sample `n ratio() + half_width()`
    /// has been processed.
    pub fn half_width(&self) -> usize {
        self.half_width
    }

    /// Decimate the next input samples of each channel (all of the same length), returning
    /// the outputs of each channel that are determined by now.
    pub fn process(&mut self, x: &[&[f32]]) -> Result<Vec<Vec<f32>>, String> {
        if self.finished {
            return Err("Decimator already finished".into());
        }
        if x.len() != self.channels.len() {
            return Err(format!(
                "Expected {} channels, got {}",
                self.channels.len(),
                x.len()
            ));
        }
        let n = x.first().map_or(0, |x| x.len());
        if x.iter().any(|x| x.len() != n) {
            return Err("Channels of different lengths".into());
        }
        let mut y = vec![Vec::new(); x.len()];
        if n == 0 {
            return Ok(y);
        }
        if self.received == 0 {
            // Before the input, the cascade sees its first sample, as many times as needed
            // for output `skip` to be at the first input sample.
            let extension = (self.skip << self.depth) - self.delay;
            for ((stages, x), y) in self.channels.iter_mut().zip(x).zip(&mut y) {
                prime(stages, x[0]);
                Self::extend(stages, x[0], extension, y, &mut self.scratch);
            }
        }
        for ((stages, x), y) in self.channels.iter_mut().zip(x).zip(&mut y) {
            cascade(stages, x, y, &mut self.scratch);
        }
        for (last, x) in self.last.iter_mut().zip(x) {
            *last = x[n - 1];
        }
        self.received += n;
        Ok(self.output(y))
    }

    /// The remaining output samples of each channel, up to the last one at or before the last
    /// input sample. The decimator takes no more input afterwards.
    pub fn finish(&mut self) -> Vec<Vec<f32>> {
        let mut y = vec![Vec::new(); self.channels.len()];
        if self.finished || self.received == 0 {
            self.finished = true;
            return y;
        }
        self.finished = true;
        let total = self.received.div_ceil(self.ratio());
        let extension = self.delay + (total << self.depth) - self.received;
        for ((stages, &last), y) in self.channels.iter_mut().zip(&self.last).zip(&mut y) {
            Self::extend(stages, last, extension, y, &mut self.scratch);
        }
        let y = self.output(y);
        debug_assert_eq!(self.emitted, total);
        y
    }

    /// Feed `value` to the cascade `count` times.
    fn extend(
        stages: &mut [Box<dyn HalfBand>],
        value: f32,
        count: usize,
        y: &mut Vec<f32>,
        scratch: &mut [Vec<f32>; 2],
    ) {
        let block = vec![value; EXTENSION_BLOCK.min(count)];
        let mut remaining = count;
        while remaining > 0 {
            let n = remaining.min(block.len());
            cascade(stages, &block[..n], y, scratch);
            remaining -= n;
        }
    }

    /// Drop the outputs before the first input sample, and normalise the gain (each stage
    /// has a gain of two).
    fn output(&mut self, mut y: Vec<Vec<f32>>) -> Vec<Vec<f32>> {
        let skip = self.skip.min(y.first().map_or(0, |y| y.len()));
        let gain = 1.0 / self.ratio() as f32;
        for y in &mut y {
            y.drain(..skip);
            for y in y.iter_mut() {
                *y *= gain;
            }
        }
        self.skip -= skip;
        self.emitted += y.first().map_or(0, |y| y.len());
        y
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decimate(depth: u32, x: &[f32], chunk: usize) -> Vec<f32> {
        let mut d = Decimator::new(depth, 1);
        let mut y = Vec::new();
        for x in x.chunks(chunk) {
            y.extend(d.process(&[x]).unwrap().remove(0));
        }
        y.extend(d.finish().remove(0));
        y
    }

    #[test]
    fn stage_delay() {
        // An impulse at an even sample comes out at the output given by the delay.
        for steps in 0..5 {
            let mut s = stage(steps);
            let mut x = vec![0.0; 4 * s.memory()];
            x[10] = 1.0;
            let mut y = Vec::new();
            s.process(&x, &mut y);
            let peak = (0..y.len())
                .max_by(|&a, &b| y[a].abs().total_cmp(&y[b].abs()))
                .unwrap();
            assert_eq!(peak, 5 + s.delay(), "stage {steps}");
            assert_eq!(y[peak], 1.0);
        }
    }

    #[test]
    fn ramp() {
        // A linear-phase filter with unit gain passes a ramp unchanged (away from the
        // ends, where the input is extended by constants).
        for depth in 0..8 {
            let r = 1 << depth;
            let x: Vec<f32> = (0..5000).map(|i| i as f32 * 1e-2).collect();
            let y = decimate(depth, &x, 777);
            assert_eq!(y.len(), x.len().div_ceil(r), "depth {depth}");
            let margin = Decimator::new(depth, 1).half_width().div_ceil(r);
            for (n, y) in y
                .iter()
                .enumerate()
                .skip(margin)
                .take(y.len().saturating_sub(2 * margin))
            {
                let expected = (n * r) as f32 * 1e-2;
                assert!(
                    (y - expected).abs() < 1e-3,
                    "depth {depth}, n {n}: {y} != {expected}"
                );
            }
        }
    }

    #[test]
    fn chunks() {
        let x: Vec<f32> = (0..20_000).map(|i| ((i * 7919) % 1000) as f32).collect();
        let whole = decimate(5, &x, x.len());
        for chunk in [1, 3, 64, 1000] {
            assert_eq!(decimate(5, &x, chunk), whole);
        }
    }

    #[test]
    fn constant() {
        let y = decimate(10, &[3.0; 5000], 1000);
        assert_eq!(y.len(), 5);
        for y in y {
            assert!((y - 3.0).abs() < 1e-5, "{y}");
        }
    }
}
