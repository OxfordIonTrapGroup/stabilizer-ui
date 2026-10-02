"""Long-term power spectral density estimates of the stream data, with the online
estimation of stabilizer-stream (through the `stabilizer_psd` bindings in `psd/`)."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass

import numpy as np
from stabilizer_psd import PsdCascade, Stage

#: Detrending methods for the segments of the estimate (value, label).
DETREND_METHODS = [("mean", "Mean"), ("midpoint", "Midpoint"), ("span", "Span"),
                   ("none", "None")]

#: First line of the CSV files written by `save_csv()`.
CSV_MAGIC = "# stabilizer-ui spectral density"


@dataclass
class Spectrum:
    """Amplitude spectral density (one-sided) of a signal."""
    name: str
    unit: str
    #: Frequencies, in Hz (without DC).
    frequencies: np.ndarray
    #: Amplitude spectral density, in `unit`/√Hz.
    asd: np.ndarray
    #: Duration of the data averaged, in seconds.
    duration: float
    #: Local time of the estimate (ISO 8601).
    timestamp: str

    def cumulative_rms(self) -> np.ndarray:
        """RMS of the signal above each frequency (the PSD integrated from the highest
        frequency down), in `unit`."""
        psd = self.asd.astype(float)**2
        segments = 0.5 * (psd[1:] + psd[:-1]) * np.diff(self.frequencies)
        return np.sqrt(np.concatenate([np.cumsum(segments[::-1])[::-1], [0]]))


class Estimate:
    """Power spectral density estimates of the sources of the stream.

    `process()` is called by the stream thread (see `StreamThread.set_consumer()`), the
    other methods by the main thread.
    """

    def __init__(self, names: list[str], units: list[str], sample_period: float,
                 max_averages: int, detrend: str):
        self.names = names
        self.units = units
        self.sample_period = sample_period
        self._cascades = [
            PsdCascade(detrend=detrend, max_averages=max_averages) for _ in names
        ]
        #: Number of samples of each source processed.
        self.samples = 0
        #: Number of samples of each source lost (left out).
        self.lost = 0

    @property
    def duration(self) -> float:
        """Duration of the data processed, in seconds."""
        return self.samples * self.sample_period

    def process(self, data: np.ndarray, lost: int):
        for cascade, samples in zip(self._cascades, data):
            cascade.process(samples)
        self.samples += data.shape[1]
        self.lost += lost

    def set_max_averages(self, max_averages: int):
        for cascade in self._cascades:
            cascade.set_averages(max_averages)

    def set_detrend(self, detrend: str):
        for cascade in self._cascades:
            cascade.set_detrend(detrend)

    def spectra(self, min_count: int,
                timestamp: str) -> tuple[list[Spectrum], list[Stage]]:
        """The spectrum of each source, from the stages with at least `min_count`
        averages, and the stages of the first source (the others have the same)."""
        spectra = []
        stages = []
        for name, unit, cascade in zip(self.names, self.units, self._cascades):
            frequencies, psd, stages_ = cascade.psd(min_count=min_count)
            stages = stages or stages_
            # Leave out DC, and convert from relative frequencies.
            frequencies = np.array(frequencies[1:]) / self.sample_period
            asd = np.sqrt(np.array(psd[1:]) * self.sample_period)
            spectra.append(
                Spectrum(name, unit, frequencies, asd, self.duration, timestamp))
        return spectra, stages


def save_csv(path: str, spectra: list[Spectrum]):
    """Save spectra as CSV, with two columns (frequency, ASD) for each, preceded by
    comment lines with the metadata (read back by `load_csv()`)."""
    metadata = [{
        "name": s.name,
        "unit": s.unit,
        "duration": s.duration,
        "timestamp": s.timestamp
    } for s in spectra]
    length = max((len(s.frequencies) for s in spectra), default=0)
    with open(path, "w", newline="") as f:
        f.write(f"{CSV_MAGIC}\n# {json.dumps(metadata)}\n")
        writer = csv.writer(f)
        writer.writerow([
            label for s in spectra for label in
            [f"{s.name} frequency (Hz)", f"{s.name} ASD ({s.unit}/sqrt(Hz))"]
        ])
        for i in range(length):
            writer.writerow([
                f"{value[i]:.7g}" if i < len(value) else "" for s in spectra
                for value in [s.frequencies, s.asd]
            ])


def load_csv(path: str) -> list[Spectrum]:
    """Load spectra saved by `save_csv()`."""
    with open(path, newline="") as f:
        if f.readline().strip() != CSV_MAGIC:
            raise ValueError("Not a spectral density file saved by stabilizer-ui")
        metadata = json.loads(f.readline().lstrip("# "))
        reader = csv.reader(f)
        next(reader)
        columns = list(zip(*reader)) or [()] * (2 * len(metadata))
    spectra = []
    for i, m in enumerate(metadata):
        frequencies, asd = (np.array([float(v) for v in columns[2 * i + j] if v])
                            for j in range(2))
        if len(frequencies) != len(asd) or not all(map(math.isfinite, frequencies)):
            raise ValueError(f"Invalid data for {m['name']}")
        spectra.append(
            Spectrum(m["name"], m["unit"], frequencies, asd, m["duration"],
                     m["timestamp"]))
    return spectra
