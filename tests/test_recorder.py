"""Recording the stream data to HDF5 files."""
import json
import time

import h5py
import numpy as np
import pytest
from stabilizer import ADC_VOLTS_PER_LSB, DAC_VOLTS_PER_LSB
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD as TS
from stabilizer.stream_parser import AdcDecoder, Parser, StabilizerStream

from stabilizer_ui.stream import recorder as recorder_module
from stabilizer_ui.stream.decoders import DacDecoder
from stabilizer_ui.stream.recorder import StreamRecorder

BATCH_SIZE = 8
BATCHES_PER_FRAME = 21
N_SOURCES = 4
#: The sources to record (ADC0 and DAC1).
SOURCES = [0, 3]


@pytest.fixture(autouse=True)
def fast_writes(monkeypatch):
    monkeypatch.setattr(recorder_module, "WRITE_INTERVAL", 0.05)


def make_parser() -> Parser:
    return Parser([AdcDecoder(), DacDecoder()])


def make_data(n_batches: int, seed=0) -> np.ndarray:
    """Raw stream data, as (batch, source, sample)."""
    rng = np.random.default_rng(seed)
    return rng.integers(-1 << 15, 1 << 15, (n_batches, N_SOURCES, BATCH_SIZE), np.int16)


def frames(raw: np.ndarray, first_sequence=0) -> list[tuple[tuple, bytes]]:
    """Split raw data into frames, as passed to `StreamRecorder.add()`."""
    result = []
    for i in range(0, len(raw), BATCHES_PER_FRAME):
        batches = raw[i:i + BATCHES_PER_FRAME]
        header = StabilizerStream.header(StabilizerStream.magic, 1, len(batches),
                                         (first_sequence + i) & 0xFFFFFFFF)
        result.append((header, batches.astype("<i2").tobytes()))
    return result


def expected(raw: np.ndarray, lost_batches=()) -> np.ndarray:
    """The recorded sources in machine units, as (source, sample)."""
    data = raw.copy()
    # DAC offset binary to two's complement (zero stays zero for lost data).
    data[:, 2:] ^= np.int16(-0x8000)
    data[list(lost_batches)] = 0
    return data.transpose(1, 0, 2).reshape(N_SOURCES, -1)[SOURCES]


def record(path, chunks, settings=None, **kwargs) -> StreamRecorder:
    """Record the given lists of frames, added one after the other."""
    recorder = StreamRecorder(str(path), make_parser(), SOURCES, TS, "test", settings,
                              **kwargs)
    for chunk in chunks:
        recorder.add(chunk)
    recorder.stop()
    recorder.wait(5)
    assert recorder.finished
    return recorder


def read(path) -> tuple[np.ndarray, np.ndarray, dict, dict]:
    """The recorded data, the lost ranges, the attributes and those of the channels."""
    with h5py.File(path, "r") as f:
        data = np.array([dataset[()] for dataset in f["channels"].values()])
        return data, f["lost"][()], dict(f.attrs), {
            name: dict(dataset.attrs)
            for name, dataset in f["channels"].items()
        }


def test_record(tmp_path):
    path = tmp_path / "rec.h5"
    raw = make_data(500)
    all_frames = frames(raw)
    settings = {"settings/afe/0": "G2"}
    recorder = record(path, [all_frames[i:i + 5] for i in range(0, len(all_frames), 5)],
                      settings)
    assert recorder.error is None
    assert recorder.samples == 500 * BATCH_SIZE and recorder.lost == 0
    assert recorder.size == 2 * len(SOURCES) * recorder.samples

    data, lost, attrs, channels = read(path)
    np.testing.assert_array_equal(data, expected(raw))
    assert lost.shape == (0, 2)
    assert list(channels) == ["ADC0", "DAC1"]
    assert channels["ADC0"]["unit"] == "V"
    assert channels["ADC0"]["scale"] == ADC_VOLTS_PER_LSB
    assert channels["DAC1"]["scale"] == DAC_VOLTS_PER_LSB
    assert attrs["format"] == recorder_module.FILE_FORMAT
    assert attrs["sample_period"] == TS and attrs["batch_size"] == BATCH_SIZE
    assert attrs["device"] == "test"
    assert json.loads(attrs["settings"]) == settings
    assert "T" in attrs["start_time"]


def test_lost_reordered_and_duplicate_frames(tmp_path):
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 40)
    all_frames = frames(raw)
    received = list(all_frames)
    # Frames 3 and 4 are lost, 10 and 11 swapped, 20 duplicated, and the last one lost.
    del received[3:5]
    received[8], received[9] = received[9], received[8]
    received.insert(18, received[18])
    del received[-1]
    # In three chunks: the duplicate is in a later chunk than the original.
    recorder = record(path, [received[:18], received[18:19], received[19:]])
    lost_batches = range(3 * BATCHES_PER_FRAME, 5 * BATCHES_PER_FRAME)
    n = 39 * BATCHES_PER_FRAME
    data, lost, _, _ = read(path)
    np.testing.assert_array_equal(data, expected(raw[:n], lost_batches))
    np.testing.assert_array_equal(lost, [[3 * 21 * BATCH_SIZE, 2 * 21 * BATCH_SIZE]])
    assert recorder.lost == 2 * 21 * BATCH_SIZE and recorder.samples == n * BATCH_SIZE


def test_late_frame_after_gap(tmp_path):
    """A frame arriving after its gap has been written stays lost."""
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 10)
    all_frames = frames(raw)
    recorder = StreamRecorder(str(path), make_parser(), SOURCES, TS)
    recorder.add(all_frames[:2] + all_frames[3:6])
    time.sleep(0.2)
    recorder.add([all_frames[2]] + all_frames[6:])
    recorder.stop()
    recorder.wait(5)
    data, lost, _, _ = read(path)
    np.testing.assert_array_equal(
        data, expected(raw, range(2 * BATCHES_PER_FRAME, 3 * BATCHES_PER_FRAME)))
    assert len(lost) == 1


def test_sequence_wraps(tmp_path):
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 10)
    record(path, [frames(raw, (1 << 32) - 50)])
    data, lost, _, _ = read(path)
    np.testing.assert_array_equal(data, expected(raw))
    assert len(lost) == 0


def test_interruption(tmp_path):
    """The recording stops after a long gap, keeping the data before it."""
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 10)
    gap = int(1.5 * recorder_module.MAX_GAP / (BATCH_SIZE * TS))
    recorder = StreamRecorder(str(path), make_parser(), SOURCES, TS)
    recorder.add(frames(raw[:105]) + frames(raw[105:], 105 + gap))
    recorder.wait(5)
    assert recorder.finished and "interrupted" in recorder.error
    data, _, _, _ = read(path)
    np.testing.assert_array_equal(data, expected(raw[:105]))


def test_restart(tmp_path):
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 10)
    start = 1 << 24
    recorder = StreamRecorder(str(path), make_parser(), SOURCES, TS)
    recorder.add(frames(raw[:105], start))
    time.sleep(0.2)
    recorder.add(frames(raw[105:]))
    recorder.wait(5)
    assert recorder.finished and "restarted" in recorder.error
    data, _, _, _ = read(path)
    np.testing.assert_array_equal(data, expected(raw[:105]))


def test_no_data(tmp_path, monkeypatch):
    """The recording stops if the stream stops."""
    monkeypatch.setattr(recorder_module, "MAX_GAP", 0.3)
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 10)
    recorder = StreamRecorder(str(path), make_parser(), SOURCES, TS)
    recorder.add(frames(raw))
    recorder.wait(5)
    assert recorder.finished and "no stream data" in recorder.error
    data, _, _, _ = read(path)
    np.testing.assert_array_equal(data, expected(raw))


def test_no_data_at_all(tmp_path):
    path = tmp_path / "rec.h5"
    recorder = record(path, [])
    assert recorder.error is None and recorder.samples == 0
    data, lost, attrs, _ = read(path)
    assert data.shape == (2, 0) and lost.shape == (0, 2)
    assert "start_time" not in attrs


def test_backlog(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder_module, "MAX_BACKLOG", 10_000)
    raw = make_data(BATCHES_PER_FRAME * 100)
    recorder = StreamRecorder(str(tmp_path / "rec.h5"), make_parser(), SOURCES, TS)
    recorder.add(frames(raw))
    recorder.wait(5)
    assert recorder.finished and "keep up" in recorder.error


def test_read_while_recording(tmp_path):
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 20)
    all_frames = frames(raw)
    recorder = StreamRecorder(str(path), make_parser(), SOURCES, TS)
    try:
        recorder.add(all_frames[:10])
        deadline = time.monotonic() + 5
        while recorder.samples == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        with h5py.File(path, "r", swmr=True) as f:
            dataset = f["channels/ADC0"]
            assert len(dataset) == 10 * BATCHES_PER_FRAME * BATCH_SIZE
            recorder.add(all_frames[10:])
            while (recorder.samples < len(raw) * BATCH_SIZE
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            dataset.refresh()
            np.testing.assert_array_equal(dataset[()], expected(raw)[0])
    finally:
        recorder.stop()
        recorder.wait(5)
