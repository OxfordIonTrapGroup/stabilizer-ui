"""Running sweeps: capturing the stream, and retaking sweeps with lost data."""

import asyncio

import numpy as np
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD as TS
from stabilizer.stream_parser import AdcDecoder, Parser, StabilizerStream

from stabilizer_ui.stream.decoders import DacDecoder
from stabilizer_ui.transfer_function import ess
from stabilizer_ui.transfer_function import measurement as measurement_module
from stabilizer_ui.transfer_function.measurement import SweepRunner

BATCH_SIZE = 8
BATCHES_PER_FRAME = 21
#: Frames fed before waiting for the trigger (enough for the pre-trigger data).
PRE_TRIGGER_FRAMES = 500


class FakeInterface:

    def __init__(self):
        self.settings = {"settings/ch/1/source/signal": "Cosine"}
        self.triggered = asyncio.Event()
        self.triggers = 0

    async def set_setting(self, key, value):
        self.settings[key] = value
        if key == "settings/trigger":
            self.triggers += 1
            self.triggered.set()

    async def get_setting(self, key):
        return self.settings.get(key)


class FakeStreamThread:
    """Feeds each capture with frames of zeros: `PRE_TRIGGER_FRAMES` before the trigger,
    then until the capture is complete. For each capture, `drops` gives the indices of
    the frames (relative to the trigger, negative before it) to leave out."""

    def __init__(self, interface: FakeInterface, drops: list[set]):
        self.interface = interface
        self.drops = drops
        self.parser = Parser([AdcDecoder(), DacDecoder()])
        self.sample_period = TS
        self.captures = 0
        self._task = None

    def start_capture(self, capture):
        drop = self.drops[self.captures] if self.captures < len(self.drops) else set()
        self.captures += 1
        self._task = asyncio.get_running_loop().create_task(self._feed(capture, drop))

    def stop_capture(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _feed(self, capture, drop):
        body = np.zeros((BATCHES_PER_FRAME, 4, BATCH_SIZE), "<i2").tobytes()
        frame = -PRE_TRIGGER_FRAMES
        while not capture.done.done():
            if frame == 0:
                await self.interface.triggered.wait()
                self.interface.triggered.clear()
            if frame not in drop:
                header = StabilizerStream.header(
                    StabilizerStream.magic, 1, BATCHES_PER_FRAME,
                    (frame + PRE_TRIGGER_FRAMES) * BATCHES_PER_FRAME)
                capture.add(header, body)
            frame += 1
            await asyncio.sleep(0)


def make_runner(drops):
    interface = FakeInterface()
    stream = FakeStreamThread(interface, drops)
    runner = SweepRunner(interface, stream, "test", lambda: [1, 1], lambda: {})
    return runner, interface, stream


def run(runner, n_runs, retake_lost):
    sweep = ess.Sweep.design(1e3, 100e3, 0.01, 0.1, TS)
    messages = []
    measurement = asyncio.run(
        runner.run(sweep, 0, n_runs, ess.default_ir_window(sweep),
                   lambda message, _: messages.append(message), retake_lost))
    return measurement, messages


def lost_frames(lost_batches):
    """The frame indices (relative to the trigger) of lost batches."""
    return set(lost_batches // BATCHES_PER_FRAME - PRE_TRIGGER_FRAMES)


def test_capture():
    runner, interface, stream = make_runner([{-3}])
    measurement, messages = run(runner, 2, retake_lost=True)
    assert stream.captures == 2 and interface.triggers == 2
    assert measurement.retakes == 0
    assert len(measurement.runs) == 2
    assert measurement.channel_names == ["ADC0", "ADC1", "DAC0", "DAC1"]
    # Data lost before the trigger does not call for a retake.
    assert lost_frames(measurement.lost_batches[0]) == {-3}
    assert len(measurement.lost_batches[1]) == 0
    assert interface.settings["settings/ch/0/source/signal"] == "SweptSine"
    assert interface.settings["settings/ch/0/source/amplitude"] == 0.0
    assert "Sweep 2 of 2…" in messages and not any("retake" in m for m in messages)


def test_retake():
    runner, interface, stream = make_runner([{10}, {-2, 5, 6}, set(), {300}])
    measurement, messages = run(runner, 2, retake_lost=True)
    assert stream.captures == 5 and interface.triggers == 5
    assert measurement.retakes == 3
    assert len(measurement.runs) == 2
    assert [len(lost) for lost in measurement.lost_batches] == [0, 0]
    assert "Sweep 1 of 2 (retake 1)…" in messages
    assert "Sweep 1 of 2 (retake 2)…" in messages
    assert "Sweep 2 of 2 (retake 1)…" in messages

    runner, interface, stream = make_runner([{10}, {-2, 5, 6}])
    measurement, messages = run(runner, 2, retake_lost=False)
    assert stream.captures == 2
    assert measurement.retakes == 0
    assert lost_frames(measurement.lost_batches[0]) == {10}
    assert lost_frames(measurement.lost_batches[1]) == {-2, 5, 6}


def test_retake_limit(monkeypatch):
    monkeypatch.setattr(measurement_module, "MAX_RETAKES", 2)
    runner, interface, stream = make_runner([{1}, {2}, {3}, {4}])
    measurement, messages = run(runner, 1, retake_lost=True)
    assert stream.captures == 3
    assert measurement.retakes == 2
    assert lost_frames(measurement.lost_batches[0]) == {3}


def test_retakes_saved(tmp_path):
    runner, _, _ = make_runner([{1}])
    measurement, _ = run(runner, 1, retake_lost=True)
    assert measurement.retakes == 1
    path = str(tmp_path / "measurement.h5")
    measurement.save(path)
    assert measurement_module.Measurement.load(path).retakes == 1
