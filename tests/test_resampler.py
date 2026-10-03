"""Streaming resampler correctness.

The decoder feeds blocks in sequence, so what matters is that the blocked
result equals the one-shot result: no drift at block joins, no repeated or
dropped frames, and a length that matches the rate ratio.
"""

from __future__ import annotations

import numpy as np
import pytest

from tracyy.audio.resampler import SOXR_AVAILABLE, StreamResampler, resample_block


def _tone(frames: int, rate: int, hz: float = 440.0) -> np.ndarray:
    t = np.arange(frames, dtype=np.float64) / rate
    left = np.sin(2 * np.pi * hz * t)
    right = np.sin(2 * np.pi * (hz * 1.5) * t)
    return np.ascontiguousarray(np.stack([left, right], axis=1), dtype=np.float32)


def _run_blocked(
    source: np.ndarray,
    source_rate: int,
    target_rate: int,
    block: int,
) -> np.ndarray:
    stream = StreamResampler(source_rate, target_rate)
    parts = []
    for start in range(0, len(source), block):
        chunk = source[start : start + block]
        out = stream.process(chunk, last=False)
        if len(out):
            parts.append(out)
    tail = stream.flush()
    if len(tail):
        parts.append(tail)
    if not parts:
        return np.zeros((0, 2), dtype=np.float32)
    return np.concatenate(parts, axis=0)


@pytest.mark.parametrize(
    ("source_rate", "target_rate"), [(44100, 48000), (48000, 44100), (96000, 48000)]
)
@pytest.mark.parametrize("block", [1024, 4096, 32771])
def test_blocked_length_matches_ratio(source_rate: int, target_rate: int, block: int) -> None:
    source = _tone(source_rate * 2, source_rate)
    produced = _run_blocked(source, source_rate, target_rate, block)
    expected = len(source) * target_rate / source_rate
    # A band-limited resampler has a filter delay; a couple of ms either way
    # is normal. Anything larger means frames are being lost at block joins.
    assert abs(len(produced) - expected) < target_rate * 0.01


@pytest.mark.parametrize("block", [1024, 8192])
def test_block_size_does_not_change_output(block: int) -> None:
    source_rate, target_rate = 44100, 48000
    source = _tone(source_rate, source_rate)
    reference = _run_blocked(source, source_rate, target_rate, len(source))
    produced = _run_blocked(source, source_rate, target_rate, block)
    span = min(len(reference), len(produced))
    assert span > target_rate * 0.5
    np.testing.assert_allclose(produced[:span], reference[:span], atol=2e-4)


def test_passthrough_is_identity() -> None:
    source = _tone(4096, 48000)
    stream = StreamResampler(48000, 48000)
    assert stream.backend == "passthrough"
    np.testing.assert_array_equal(stream.process(source), source)


def test_linear_fallback_tracks_the_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback is not band-limited, but it must still be the same tone."""
    monkeypatch.setenv("TRACYY_LINEAR_RESAMPLE", "1")
    source_rate, target_rate = 44100, 48000
    source = _tone(source_rate, source_rate, hz=200.0)
    produced = _run_blocked(source, source_rate, target_rate, 4096)

    expected_frames = int(len(source) * target_rate / source_rate)
    assert abs(len(produced) - expected_frames) <= 2

    # flush() holds the final source sample so the tail is not truncated, so
    # the very last output frame is a repeat rather than a continuation.
    produced = produced[:-1]
    t = np.arange(len(produced), dtype=np.float64) / target_rate
    ideal = np.sin(2 * np.pi * 200.0 * t).astype(np.float32)
    # Linear interpolation error at 200 Hz is tiny; a timing slip would not be.
    assert float(np.max(np.abs(produced[:, 0] - ideal))) < 0.01


@pytest.mark.skipif(not SOXR_AVAILABLE, reason="soxr not installed")
def test_one_shot_matches_blocked() -> None:
    source_rate, target_rate = 44100, 48000
    source = _tone(source_rate, source_rate)
    one_shot = resample_block(source, source_rate, target_rate)
    blocked = _run_blocked(source, source_rate, target_rate, 4096)
    span = min(len(one_shot), len(blocked))
    np.testing.assert_allclose(blocked[:span], one_shot[:span], atol=2e-4)
