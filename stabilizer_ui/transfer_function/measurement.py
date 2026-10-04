"""Running transfer function measurements, and storing them."""

from __future__ import annotations

import asyncio
import csv
import datetime
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Callable

import h5py
import numpy as np

from . import ess
from ..interface import AbstractStabilizerInterface
from ..stream.decoders import source_scales, to_machine_units
from ..stream.thread import StreamCapture, StreamThread

logger = logging.getLogger(__name__)

FILE_FORMAT = "stabilizer-ui transfer function measurement"
FILE_VERSION = 1

#: Data captured before triggering the sweep, in seconds.
PRE_TRIGGER = 0.1
#: Additional data captured after the expected end (for the latency of the trigger), in
#: seconds and relative to the sweep duration.
POST_TRIGGER_MARGIN = (0.3, 0.1)
#: Time to wait for the first stream data, in seconds.
STREAM_TIMEOUT = 2.0
#: How often a sweep is retaken when stream data was lost after the trigger (if
#: requested) before the capture with the fewest lost batches is kept.
MAX_RETAKES = 5


def post_trigger_duration(sweep: ess.Sweep, ir_window: float) -> float:
    """Time to capture after triggering the sweep, in seconds."""
    margin, relative_margin = POST_TRIGGER_MARGIN
    return ((1 + relative_margin) * sweep.duration + ess.capture_tail(ir_window) + margin)


@dataclass
class Measurement:
    """The captured data of a transfer function measurement, and its analysis."""

    sweep: ess.Sweep
    #: The excited channel.
    channel: int
    #: Names of the captured channels (stream sources, e.g. ADC0, ADC1, DAC0, DAC1).
    channel_names: list[str]
    #: Scale of the raw data to volts (input-referred for ADCs), for each channel.
    scales: np.ndarray
    batch_size: int
    #: Captured raw data, as (channel, sample) arrays of machine units, for each run.
    runs: list[np.ndarray]
    #: Indices of the batches lost in each run (interpolated).
    lost_batches: list[np.ndarray]
    device: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    #: Snapshot of the device and UI settings (topic path to value).
    settings: dict = field(default_factory=dict)
    #: Number of sweeps retaken because stream data was lost.
    retakes: int = 0
    name: str = ""
    analysis_settings: ess.AnalysisSettings | None = None
    analysis: ess.Analysis | None = None

    @property
    def reference(self) -> int:
        """Index of the channel containing the stimulus (the excited channel's DAC)."""
        return self.channel_names.index(f"DAC{self.channel}")

    @property
    def response_names(self) -> list[str]:
        """Names of the channels of the analysis results."""
        return self.channel_names + [ess.filter_output_name(self.channel)]

    @property
    def label(self) -> str:
        return self.name or f"{self.timestamp} (channel {self.channel})"

    def volts(self, run: int, interpolate: bool = True) -> np.ndarray:
        """The data of a run in volts, with lost batches interpolated (or NaN if not
        `interpolate`)."""
        data = self.runs[run] * self.scales[:, np.newaxis]
        lost = self.lost_batches[run]
        if len(lost):
            n = data.shape[1]
            mask = np.zeros(n, bool)
            for batch in lost:
                mask[batch * self.batch_size:(batch + 1) * self.batch_size] = True
            if not interpolate:
                data[:, mask] = np.nan
                return data
            index = np.arange(n)
            for channel in data:
                channel[mask] = np.interp(index[mask], index[~mask], channel[~mask])
        return data

    def gaps(self, run: int) -> np.ndarray:
        """The ranges of samples (start, stop) of a run which were lost (and are
        interpolated by `volts()`), as an (n, 2) array."""
        lost = np.sort(self.lost_batches[run])
        if not len(lost):
            return np.zeros((0, 2), int)
        breaks = np.flatnonzero(np.diff(lost) > 1) + 1
        starts = lost[np.concatenate([[0], breaks])]
        stops = lost[np.concatenate([breaks - 1, [len(lost) - 1]])] + 1
        return np.column_stack([starts, stops]) * self.batch_size

    def analyse(self, settings: ess.AnalysisSettings) -> ess.Analysis:
        offsets = self.analysis.offsets if self.analysis is not None else None
        runs = range(len(self.runs))
        analysis = ess.analyse([self.volts(i) for i in runs], self.sweep, self.reference,
                               self.batch_size, settings, offsets,
                               [self.gaps(i) for i in runs])
        self.analysis_settings = settings
        self.analysis = analysis
        return analysis

    def responses(self) -> tuple[dict, dict]:
        """The analysed responses and their noise, by channel name."""
        names = self.response_names
        return (dict(zip(names,
                         self.analysis.responses)), dict(zip(names, self.analysis.noise)))

    def quantity(self, quantity: str) -> tuple[np.ndarray, np.ndarray]:
        """One of `ess.QUANTITIES` and its noise estimate."""
        return ess.derived_quantity(quantity, *self.responses(), self.channel)

    def harmonic(self, quantity: str,
                 k: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """The fundamental frequencies, the k-th harmonic response, and its noise
        estimate corresponding to one of `ess.QUANTITIES` (see `ess.derived_harmonic()`).
        """
        analysis, names = self.analysis, self.response_names
        value, noise = ess.derived_harmonic(quantity,
                                            dict(zip(names, analysis.harmonics[k])),
                                            dict(zip(names, analysis.harmonic_noise[k])),
                                            self.responses()[0], analysis.frequencies,
                                            analysis.harmonic_frequencies[k],
                                            self.channel)
        return analysis.harmonic_frequencies[k], value, noise

    def save(self, path: str):
        with h5py.File(path, "w") as f:
            f.attrs["format"] = FILE_FORMAT
            f.attrs["version"] = FILE_VERSION
            f.attrs["name"] = self.name
            f.attrs["device"] = self.device
            f.attrs["timestamp"] = self.timestamp
            f.attrs["settings"] = json.dumps(self.settings)
            f.attrs["excited_channel"] = self.channel
            f.attrs["channel_names"] = self.channel_names
            f.attrs["batch_size"] = self.batch_size
            f.attrs["retakes"] = self.retakes

            sweep = f.create_group("sweep")
            sweep.attrs["description"] = (
                "Exponential sweep of the firmware SweptSine signal source, added to the "
                "DAC output of the excited channel: amplitude * sin(2 pi phase[n]) for "
                "0 <= n < length, with phase[n] = cycles * ((1 + rate / 2**32)**n - 1) "
                "turns (up to fixed-point rounding), cycles = state / (2**32 rate).")
            for key in ["rate", "state", "length", "amplitude", "sample_period"]:
                sweep.attrs[key] = getattr(self.sweep, key)
            for key in ["f_start", "f_stop", "duration", "cycles"]:
                sweep.attrs[key] = getattr(self.sweep, key)

            runs = f.create_group("runs")
            runs.attrs["description"] = (
                "Captured stream data in machine units (two's complement), as (channel, "
                "sample). Multiply by `scales` for volts (input-referred for ADCs).")
            runs.create_dataset("scales", data=self.scales)
            for i, (data, lost) in enumerate(zip(self.runs, self.lost_batches)):
                run = runs.create_dataset(str(i), data=data, compression="gzip")
                run.attrs["lost_batches"] = lost

            if self.analysis is not None:
                self._save_analysis(f.create_group("analysis"))

    def _save_analysis(self, group):
        analysis, settings = self.analysis, self.analysis_settings
        group.attrs["description"] = (
            "Responses of each channel relative to the stimulus (V/V), on a logarithmic "
            "frequency grid. The last channel is the excited channel's DAC output "
            "without the stimulus (IIR filter output). `noise` is the 1 sigma "
            "uncertainty.")
        group.attrs["channel_names"] = self.response_names
        group.attrs["ir_window"] = settings.ir_window
        group.attrs["points_per_decade"] = settings.points_per_decade
        group.attrs["max_harmonic"] = settings.max_harmonic
        group.attrs["offsets"] = analysis.offsets
        group.attrs["warnings"] = json.dumps(analysis.warnings)
        group.create_dataset("frequencies", data=analysis.frequencies)
        group.create_dataset("responses", data=analysis.responses)
        group.create_dataset("noise", data=analysis.noise)
        group.create_dataset("ir_time", data=analysis.ir_time)
        group.create_dataset("impulse_responses",
                             data=analysis.impulse_responses.astype(np.float32),
                             compression="gzip")
        group.attrs["ir_window_span"] = analysis.ir_window
        if analysis.noise_window is not None:
            group.attrs["noise_window_span"] = analysis.noise_window
        harmonics = group.create_group("harmonics")
        for k, values in analysis.harmonics.items():
            harmonic = harmonics.create_group(str(k))
            harmonic.create_dataset("frequencies", data=analysis.harmonic_frequencies[k])
            harmonic.create_dataset("responses", data=values)
            harmonic.create_dataset("noise", data=analysis.harmonic_noise[k])
            harmonic.attrs["window_span"] = analysis.harmonic_windows[k]
        derived = group.create_group("derived")
        for quantity in ess.QUANTITIES:
            value, noise = self.quantity(quantity)
            dataset = derived.create_dataset(quantity, data=value)
            dataset.attrs["label"] = ess.quantity_label(quantity, self.channel)
            derived.create_dataset(f"{quantity}_noise", data=noise)
            for k in analysis.harmonics:
                _, value, noise = self.harmonic(quantity, k)
                dataset = derived.create_dataset(f"{quantity}_h{k}", data=value)
                dataset.attrs["description"] = (
                    f"Harmonic {k} at {k} times the frequencies "
                    f"`harmonics/{k}/frequencies`, normalised like the fundamental (only "
                    "the magnitude is meaningful)")
                derived.create_dataset(f"{quantity}_h{k}_noise", data=noise)

    @classmethod
    def load(cls, path: str) -> Measurement:
        with h5py.File(path, "r") as f:
            if f.attrs.get("format") != FILE_FORMAT:
                raise ValueError("Not a transfer function measurement file")
            if f.attrs["version"] > FILE_VERSION:
                raise ValueError("Unsupported file version")
            sweep = f["sweep"].attrs
            runs = f["runs"]
            n_runs = len([key for key in runs if key.isdigit()])
            measurement = cls(
                sweep=ess.Sweep(int(sweep["rate"]), int(sweep["state"]),
                                int(sweep["length"]), float(sweep["amplitude"]),
                                float(sweep["sample_period"])),
                channel=int(f.attrs["excited_channel"]),
                channel_names=[str(name) for name in f.attrs["channel_names"]],
                scales=runs["scales"][()],
                batch_size=int(f.attrs["batch_size"]),
                runs=[runs[str(i)][()] for i in range(n_runs)],
                lost_batches=[runs[str(i)].attrs["lost_batches"] for i in range(n_runs)],
                device=str(f.attrs["device"]),
                timestamp=str(f.attrs["timestamp"]),
                settings=json.loads(f.attrs["settings"]),
                retakes=int(f.attrs.get("retakes", 0)),
                name=str(f.attrs["name"]),
            )
            if "analysis" in f:
                measurement._load_analysis(f["analysis"])
        return measurement

    def _load_analysis(self, group):
        self.analysis_settings = ess.AnalysisSettings(
            ir_window=float(group.attrs["ir_window"]),
            points_per_decade=int(group.attrs["points_per_decade"]),
            max_harmonic=int(group.attrs["max_harmonic"]))
        harmonics = group["harmonics"]
        noise_window = group.attrs.get("noise_window_span")
        self.analysis = ess.Analysis(
            frequencies=group["frequencies"][()],
            responses=group["responses"][()],
            noise=group["noise"][()],
            harmonic_frequencies={
                int(k): harmonics[k]["frequencies"][()]
                for k in harmonics
            },
            harmonics={int(k): harmonics[k]["responses"][()]
                       for k in harmonics},
            # Not stored by earlier versions.
            harmonic_noise={
                int(k):
                harmonics[k]["noise"][()] if "noise" in harmonics[k] else np.full(
                    harmonics[k]["responses"].shape, np.nan)
                for k in harmonics
            },
            ir_time=group["ir_time"][()],
            impulse_responses=group["impulse_responses"][()].astype(float),
            ir_window=tuple(group.attrs["ir_window_span"]),
            noise_window=tuple(noise_window) if noise_window is not None else None,
            harmonic_windows={
                int(k): tuple(harmonics[k].attrs["window_span"])
                for k in harmonics
            },
            offsets=[int(offset) for offset in group.attrs["offsets"]],
            warnings=json.loads(group.attrs["warnings"]),
        )

    def export_csv(self, path: str, quantity: str):
        """Write a derived quantity (see `ess.QUANTITIES`) and its harmonics as CSV."""
        f = self.analysis.frequencies
        value, noise = self.quantity(quantity)
        with np.errstate(divide="ignore"):
            magnitude = 20 * np.log10(np.abs(value))
        columns = {
            "frequency_Hz": f,
            "magnitude_dB": magnitude,
            "phase_deg": np.degrees(np.unwrap(np.angle(value))),
            "real": value.real,
            "imag": value.imag,
            "noise_abs": noise,
        }
        # The harmonics (as functions of the fundamental frequency) are available on a
        # subset of the frequencies.
        for k in self.analysis.harmonics:
            harmonic_f, harmonic, harmonic_noise = self.harmonic(quantity, k)
            index = np.searchsorted(f, harmonic_f)
            for name, data in [(f"h{k}_abs", np.abs(harmonic)),
                               (f"h{k}_noise_abs", harmonic_noise)]:
                columns[name] = np.full(len(f), np.nan)
                columns[name][index] = data
        with open(path, "w", newline="") as file:
            file.write(f"# {self.label}: {ess.quantity_label(quantity, self.channel)}\n")
            if self.analysis.harmonics:
                file.write("# hK: harmonic K (at K times the frequency), normalised like "
                           "the fundamental\n")
            writer = csv.writer(file)
            writer.writerow(columns)
            for row in zip(*columns.values()):
                writer.writerow([f"{x:.9g}" for x in row])


class SweepRunner:
    """Runs transfer function measurements: configures and triggers the signal source of
    the device, and captures the stream data."""

    def __init__(self, interface: AbstractStabilizerInterface,
                 stream_thread: StreamThread, device: str,
                 afe_gains: Callable[[], list[int]], settings: Callable[[], dict]):
        """
        :param afe_gains: Returns the current AFE gain of each channel.
        :param settings: Returns a snapshot of the current settings to store.
        """
        self.interface = interface
        self.stream_thread = stream_thread
        self.device = device
        self.afe_gains = afe_gains
        self.settings = settings

    @property
    def sample_period(self) -> float:
        return self.stream_thread.sample_period

    async def _set_source(self, channel: int, config: dict):
        for key, value in config.items():
            await self.interface.set_setting(f"settings/ch/{channel}/source/{key}", value)

    async def _stop_source(self, channel: int, stop_running: bool):
        """Disable the source, so that it does not run again on the next trigger (of
        either channel). If `stop_running`, also stop the current output."""
        await self.interface.set_setting(f"settings/ch/{channel}/source/amplitude", 0.0)
        if stop_running:
            await self.interface.set_setting("settings/trigger", True)

    async def run(self,
                  sweep: ess.Sweep,
                  channel: int,
                  n_runs: int,
                  ir_window: float,
                  progress: Callable[[str, float], None] = lambda *_: None,
                  retake_lost: bool = False) -> Measurement:
        """Measure the response to `n_runs` sweeps on the given channel.

        :param ir_window: The impulse response window that will be used for the
            analysis, which determines how long to capture after the sweep.
        :param progress: Called with a status message and the fraction completed.
        :param retake_lost: Repeat a sweep if stream data was lost after it was
            triggered (up to `MAX_RETAKES` times per sweep, after which the capture
            with the fewest lost batches is kept), instead of leaving the analysis to
            fill it in.
        """
        loop = asyncio.get_running_loop()
        parser = self.stream_thread.parser
        names = list(parser.StreamData._fields)
        if f"DAC{channel}" not in names:
            raise ValueError("The stream does not contain the DAC output")

        scales = source_scales(parser)
        gains = self.afe_gains()
        for i, name in enumerate(names):
            if name.startswith("ADC"):
                scales[i] /= gains[int(name[3:])]
        settings = self.settings()

        # A previous sweep on the other channel would run again on the trigger.
        other = 1 - channel
        if (await self.interface.get_setting(f"settings/ch/{other}/source/signal") ==
                "SweptSine"):
            logger.info("Disabling previous sweep on channel %d", other)
            await self._stop_source(other, False)

        progress("Configuring signal source…", 0)
        await self._set_source(channel, sweep.source_config())

        runs, lost_batches, batch_size = [], [], None
        running = False
        run, retaken, retakes = 0, 0, 0
        # The capture of the current sweep with the fewest batches lost after the
        # trigger so far: (number lost, data, lost batches).
        best = None
        try:
            while run < n_runs:
                label = f"Sweep {run + 1} of {n_runs}"
                if retaken:
                    label += f" (retake {retaken})"
                capture = StreamCapture(loop, parser.n_sources)
                self.stream_thread.start_capture(capture)
                try:
                    try:
                        await asyncio.wait_for(capture.started.wait(), STREAM_TIMEOUT)
                    except asyncio.TimeoutError:
                        raise RuntimeError("No stream data received") from None
                    batch_period = capture.batch_size * self.sample_period
                    while capture.batches * batch_period < PRE_TRIGGER:
                        await asyncio.sleep(0.01)

                    progress(f"{label}…", run / n_runs)
                    running = True
                    # The sweep starts after the trigger is acknowledged, so losses
                    # from here on can affect it.
                    trigger_batch = capture.batches
                    await self.interface.set_setting("settings/trigger", True)
                    # The sweep starts right after the trigger is acknowledged.
                    duration = post_trigger_duration(sweep, ir_window)
                    capture.stop_after(math.ceil(duration / batch_period))

                    async def report():
                        start = capture.batches
                        while True:
                            done = (capture.batches - start) / (capture.target - start)
                            progress(f"{label}…", (run + min(done, 1)) / n_runs)
                            await asyncio.sleep(0.1)

                    reporter = asyncio.create_task(report())
                    try:
                        await asyncio.wait_for(capture.done, duration + 10)
                    except asyncio.TimeoutError:
                        raise RuntimeError(
                            "Stream data stopped during the sweep") from None
                    finally:
                        reporter.cancel()
                    running = False
                finally:
                    self.stream_thread.stop_capture()

                data, lost = capture.assemble()
                affected = np.sum(lost >= trigger_batch)
                if best is None or affected < best[0]:
                    best = (affected, data, lost)
                if affected and retake_lost:
                    if retaken < MAX_RETAKES:
                        retaken += 1
                        retakes += 1
                        logger.info("Retaking sweep %d: %d stream batches lost", run + 1,
                                    affected)
                        continue
                    affected, data, lost = best
                    logger.warning(
                        "Keeping sweep %d with %d stream batches lost (the fewest) after "
                        "%d retakes", run + 1, affected, retaken)
                elif affected:
                    logger.warning("%d stream batches lost during sweep %d", affected,
                                   run + 1)
                batch_size = capture.batch_size
                to_machine_units(parser, data)
                runs.append(data)
                lost_batches.append(lost)
                run, retaken, best = run + 1, 0, None
        finally:
            try:
                await self._stop_source(channel, running)
            except Exception as e:
                logger.warning("Failed to disable the signal source: %s", e)

        progress("Captured", 1)
        return Measurement(sweep=sweep,
                           channel=channel,
                           channel_names=names,
                           scales=scales,
                           batch_size=batch_size,
                           runs=runs,
                           lost_batches=lost_batches,
                           device=self.device,
                           settings=settings,
                           retakes=retakes)
