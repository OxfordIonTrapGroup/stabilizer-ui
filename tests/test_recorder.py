"""Recording the stream data to HDF5 files."""
import json
import time

import h5py
import numpy as np
import pytest
from stabilizer import ADC_VOLTS_PER_LSB, DAC_VOLTS_PER_LSB
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD as TS
from stabilizer.stream_parser import AdcDecoder, Parser, StabilizerStream

from stabilizer_ui.stream import decimation, recorder as recorder_module
from stabilizer_ui.stream.decimation import Decimation
from stabilizer_ui.stream.decoders import DacDecoder
from stabilizer_ui.stream.recorder import StreamRecorder

needs_psd = pytest.mark.skipif(not decimation.available(),
                               reason="needs the stabilizer_dsp extension")

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


def decimated(raw: np.ndarray, ratio: int,
              lost_batches=()) -> tuple[np.ndarray, list, list]:
    """The recorded sources decimated by `ratio` in one go, and the lost and extrapolated
    ranges as (first, number)."""
    lost = np.zeros(len(raw) + 1, bool)
    lost[list(lost_batches)] = True
    edges = np.flatnonzero(np.diff(lost, prepend=False))
    decimation = Decimation(ratio, [None] * len(SOURCES))
    data, ranges = decimation.process(expected(raw, lost_batches),
                                      edges.reshape(-1, 2) * BATCH_SIZE)
    tail, extrapolated = decimation.finish()
    data = np.concatenate([data, tail], axis=1)
    n = data.shape[1]
    lost = [[first, min(stop, n) - first] for first, stop in ranges]
    if decimation.edge >= n - extrapolated:
        return data, lost, [[0, n]]
    return data, lost, [[0, decimation.edge], [n - extrapolated, extrapolated]]


def record(path, chunks, settings=None, interval=0.0, **kwargs) -> StreamRecorder:
    """Record the given lists of frames, added one after the other (`interval` seconds
    apart)."""
    recorder = StreamRecorder(str(path), make_parser(), SOURCES, TS, "test", settings,
                              **kwargs)
    for chunk in chunks:
        recorder.add(chunk)
        time.sleep(interval)
    recorder.stop()
    recorder.wait(5)
    assert recorder.finished
    return recorder


def read_extrapolated(path) -> list:
    with h5py.File(path, "r") as f:
        return f["extrapolated"][()].tolist()


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
    assert attrs["decimation"] == 1 and read_extrapolated(path) == []


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


def test_time_limit(tmp_path):
    """The recording stops by itself at the time limit (within a batch), also if the
    stream is interrupted after it."""
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 10)
    gap = int(1.5 * recorder_module.MAX_GAP / (BATCH_SIZE * TS))
    received = frames(raw[:105]) + frames(raw[105:], 105 + gap)
    # Frame 2 is lost, and the limit is in frame 4.
    del received[2]
    n = 4 * BATCHES_PER_FRAME * BATCH_SIZE + 13
    recorder = StreamRecorder(str(path), make_parser(), SOURCES, TS, time_limit=n * TS)
    assert recorder.time_limit == n * TS
    recorder.add(received[:3])
    time.sleep(0.2)
    recorder.add(received[3:])
    recorder.wait(5)
    assert recorder.finished and recorder.error is None
    assert recorder.samples == n and recorder.duration == n * TS
    data, lost, _, _ = read(path)
    lost_batches = range(2 * BATCHES_PER_FRAME, 3 * BATCHES_PER_FRAME)
    np.testing.assert_array_equal(data, expected(raw, lost_batches)[:, :n])
    assert lost.tolist() == [[2 * 21 * BATCH_SIZE, 21 * BATCH_SIZE]]


def test_time_limit_in_lost_data(tmp_path):
    """Lost data up to the time limit is counted up to there."""
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 4)
    received = frames(raw)
    del received[2]
    n = 2 * BATCHES_PER_FRAME * BATCH_SIZE + 5
    recorder = record(path, [received], time_limit=n * TS)
    assert recorder.error is None and recorder.samples == n and recorder.lost == 5
    data, lost, _, _ = read(path)
    np.testing.assert_array_equal(data, expected(raw, [2 * BATCHES_PER_FRAME])[:, :n])
    assert lost.tolist() == [[n - 5, 5]]


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


@needs_psd
def test_record_decimated(tmp_path):
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 40)
    received = frames(raw)
    # Frames 3 and 4, and 10 are lost.
    del received[10]
    del received[3:5]
    # In several writes.
    recorder = record(path, [received[i:i + 6] for i in range(0, len(received), 6)],
                      interval=0.1,
                      decimation=16)
    assert recorder.error is None
    n = len(raw) * BATCH_SIZE
    assert recorder.samples == n and recorder.lost == 3 * BATCHES_PER_FRAME * BATCH_SIZE
    assert recorder.size == 4 * len(SOURCES) * n // 16

    data, lost, attrs, channels = read(path)
    lost_batches = [
        *range(3 * BATCHES_PER_FRAME, 5 * BATCHES_PER_FRAME),
        *range(10 * BATCHES_PER_FRAME, 11 * BATCHES_PER_FRAME)
    ]
    expected_data, expected_lost, expected_extrapolated = decimated(raw, 16, lost_batches)
    assert data.dtype == np.float32
    np.testing.assert_array_equal(data, expected_data)
    np.testing.assert_array_equal(lost, expected_lost)
    assert read_extrapolated(path) == expected_extrapolated
    # The filter spans 461 stream samples on either side: samples 0 (at 0) to 28 (at 448)
    # reach before the start, 392 (at 6272) to 419 after the last stream sample (6719).
    assert expected_extrapolated == [[0, 29], [392, 28]]
    # Stream samples 504 to 839 are lost, so outputs 31 (at 496) to 53 (at 848), and 1680
    # to 1847, so 105 (at 1680) to 116 (at 1856).
    assert lost.tolist() == [[31, 23], [105, 12]]
    assert attrs["decimation"] == 16 and attrs["sample_period"] == 16 * TS
    assert channels["ADC0"]["scale"] == ADC_VOLTS_PER_LSB


@needs_psd
def test_lost_at_end_decimated(tmp_path):
    """Lost ranges end at the last sample written."""
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 10)
    received = frames(raw)
    del received[8]
    record(path, [received], decimation=1024)
    data, lost, _, _ = read(path)
    # 1680 samples: at 0 and 1024; samples 1344 to 1511 are lost.
    assert data.shape == (2, 2)
    assert lost.tolist() == [[1, 1]]
    # Both reach beyond either end.
    assert read_extrapolated(path) == [[0, 2]]
    np.testing.assert_array_equal(
        data,
        decimated(raw, 1024, range(8 * BATCHES_PER_FRAME, 9 * BATCHES_PER_FRAME))[0])


@needs_psd
def test_time_limit_decimated(tmp_path):
    """With decimation, the time limit is rounded to whole samples of the recording."""
    path = tmp_path / "rec.h5"
    raw = make_data(BATCHES_PER_FRAME * 10)
    ratio, n = 4, 101
    # 404 stream samples, ending within a batch (101.4 samples rounded down).
    recorder = record(path, [frames(raw)],
                      decimation=ratio,
                      time_limit=101.4 * ratio * TS)
    assert recorder.error is None and recorder.samples == ratio * n
    assert recorder.time_limit == ratio * n * TS
    decimation = Decimation(ratio, [None] * len(SOURCES))
    reference, _ = decimation.process(expected(raw)[:, :ratio * n])
    tail, extrapolated = decimation.finish()
    reference = np.concatenate([reference, tail], axis=1)
    data, lost, _, _ = read(path)
    assert data.shape == (len(SOURCES), n) and len(lost) == 0
    np.testing.assert_array_equal(data, reference)
    assert read_extrapolated(path) == [[0, decimation.edge],
                                       [n - extrapolated, extrapolated]]


def test_invalid_decimation(tmp_path):
    path = tmp_path / "rec.h5"
    with pytest.raises((ValueError, RuntimeError)):
        StreamRecorder(str(path), make_parser(), SOURCES, TS, decimation=3)
    assert not path.exists()
