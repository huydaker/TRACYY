"""End-to-end checks for the decoder process and the look-ahead window."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tracyy.audio.streaming import StreamingAudioSource, probe_audio

SOURCE_RATE = 44100
SECONDS = 6


@pytest.fixture(scope="module")
def tone_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("audio") / "tone.wav"
    t = np.arange(SOURCE_RATE * SECONDS, dtype=np.float64) / SOURCE_RATE
    # A frequency ramp, so any position in the file is identifiable.
    left = np.sin(2 * np.pi * (100.0 + 40.0 * t) * t)
    right = np.cos(2 * np.pi * (100.0 + 40.0 * t) * t)
    sf.write(path, np.stack([left, right], axis=1).astype(np.float32), SOURCE_RATE)
    return path


def _wait_ready(source: StreamingAudioSource, frame: int = 0, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if source.error_message():
            pytest.fail(source.error_message())
        if source.ready(frame):
            return True
        time.sleep(0.02)
    return False


def test_probe_reports_metadata(tone_file: Path) -> None:
    probe = probe_audio(str(tone_file))
    assert probe["sample_rate"] == SOURCE_RATE
    assert probe["decoder"] == "soundfile"
    assert probe["duration"] == pytest.approx(SECONDS, abs=0.01)


def test_streams_at_native_rate(tone_file: Path) -> None:
    source = StreamingAudioSource(str(tone_file), buffer_seconds=8.0, startup_seconds=1.0)
    try:
        source.start(SOURCE_RATE, 0.0)
        assert _wait_ready(source), "decoder never became ready"

        destination = np.zeros((2048, 2), dtype=np.float32)
        copied, ended = source.read_into(destination, 0)
        assert copied == 2048
        assert not ended
        assert float(np.max(np.abs(destination))) > 0.5
    finally:
        source.close()


def test_streams_resampled(tone_file: Path) -> None:
    source = StreamingAudioSource(str(tone_file), buffer_seconds=8.0, startup_seconds=1.0)
    try:
        source.start(48000, 0.0)
        assert _wait_ready(source)
        assert source.output_rate == 48000
        assert len(source) == pytest.approx(SECONDS * 48000, rel=0.01)

        destination = np.zeros((4096, 2), dtype=np.float32)
        copied, _ended = source.read_into(destination, 0)
        assert copied == 4096
        assert float(np.max(np.abs(destination))) > 0.5
    finally:
        source.close()


def test_seek_reuses_the_decoder_process(tone_file: Path) -> None:
    """The whole point of the persistent worker: a scrub must not respawn it."""
    source = StreamingAudioSource(str(tone_file), buffer_seconds=8.0, startup_seconds=0.5)
    try:
        source.start(48000, 0.0)
        assert _wait_ready(source)
        first_pid = source._process.pid

        for target in (3.0, 1.0, 4.5, 0.25):
            frame = int(target * 48000)
            source.start(48000, target)
            source.notify_position(frame)
            assert _wait_ready(source, frame), f"seek to {target}s never buffered"
            assert source._process.pid == first_pid, "decoder process was respawned"

            destination = np.zeros((1024, 2), dtype=np.float32)
            copied, _ended = source.read_into(destination, frame)
            assert copied == 1024
    finally:
        source.close()


def test_reads_before_the_window_are_refused(tone_file: Path) -> None:
    source = StreamingAudioSource(str(tone_file), buffer_seconds=8.0, startup_seconds=0.5)
    try:
        source.start(48000, 4.0)
        frame = int(4.0 * 48000)
        assert _wait_ready(source, frame)
        destination = np.ones((512, 2), dtype=np.float32)
        # Frame 0 was never decoded for this generation.
        copied, _ended = source.read_into(destination, 0)
        assert copied == 0
        assert float(np.max(np.abs(destination))) == 0.0, "buffer must be zeroed"
    finally:
        source.close()


def test_eof_is_reported(tone_file: Path) -> None:
    source = StreamingAudioSource(
        str(tone_file),
        buffer_seconds=30.0,
        startup_seconds=0.5,
    )
    try:
        near_end = int((SECONDS - 0.05) * SOURCE_RATE)
        source.start(SOURCE_RATE, SECONDS - 0.05)
        source.notify_position(near_end)
        assert _wait_ready(source, near_end)

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not source._eof:
            time.sleep(0.02)
        assert source._eof

        destination = np.zeros((8192, 2), dtype=np.float32)
        _copied, ended = source.read_into(destination, near_end)
        assert ended
    finally:
        source.close()
