"""Waveform cache addressing, invalidation and pruning."""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest

from tracyy.media.waveform_cache import WaveformCache


@pytest.fixture
def cache(tmp_path: Path) -> WaveformCache:
    return WaveformCache(tmp_path / "waveforms")


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "track.wav"
    path.write_bytes(b"RIFF" + b"\x00" * 1024)
    return path


def test_round_trip(cache: WaveformCache, audio: Path) -> None:
    preview = np.linspace(0.0, 1.0, 4096, dtype=np.float32)
    assert cache.store(audio, 4096, preview)
    loaded = cache.load(audio, 4096)
    assert loaded is not None
    np.testing.assert_array_equal(loaded, preview)


def test_missing_entry_returns_none(cache: WaveformCache, audio: Path) -> None:
    assert cache.load(audio, 4096) is None


def test_different_resolutions_are_separate(cache: WaveformCache, audio: Path) -> None:
    cache.store(audio, 4096, np.zeros(4096, dtype=np.float32))
    assert cache.load(audio, 8192) is None


def test_edited_source_invalidates_the_entry(cache: WaveformCache, audio: Path) -> None:
    cache.store(audio, 4096, np.ones(4096, dtype=np.float32))
    assert cache.load(audio, 4096) is not None

    # Same path, different content: the size/mtime key must not match.
    time.sleep(0.01)
    audio.write_bytes(b"RIFF" + b"\x01" * 2048)
    assert cache.load(audio, 4096) is None


def test_corrupt_entry_is_discarded(cache: WaveformCache, audio: Path) -> None:
    cache.store(audio, 4096, np.ones(4096, dtype=np.float32))
    target = cache.path_for(audio, 4096)
    assert target is not None
    target.write_bytes(b"not a numpy file")

    assert cache.load(audio, 4096) is None
    assert not target.exists(), "a corrupt entry should be removed, not retried forever"


def test_missing_source_has_no_key(cache: WaveformCache, tmp_path: Path) -> None:
    assert cache.path_for(tmp_path / "nope.wav", 4096) is None


def test_prune_by_size_keeps_the_most_recently_used(
    cache: WaveformCache, tmp_path: Path
) -> None:
    sources = []
    for index in range(6):
        source = tmp_path / f"track{index}.wav"
        source.write_bytes(b"RIFF" + bytes([index]) * 512)
        sources.append(source)
        cache.store(source, 4096, np.zeros(4096, dtype=np.float32))

    entry_size = cache.total_bytes() // 6
    # Mark the last two as recently used, the rest as old.
    old = time.time() - 3600
    for source in sources[:4]:
        path = cache.path_for(source, 4096)
        assert path is not None
        os.utime(path, (old, old))

    removed = cache.prune(max_bytes=entry_size * 2, max_age_days=365)
    assert removed == 4
    assert cache.load(sources[4], 4096) is not None
    assert cache.load(sources[5], 4096) is not None


def test_prune_by_age(cache: WaveformCache, audio: Path) -> None:
    cache.store(audio, 4096, np.zeros(4096, dtype=np.float32))
    path = cache.path_for(audio, 4096)
    assert path is not None
    ancient = time.time() - 200 * 86400
    os.utime(path, (ancient, ancient))

    assert cache.prune(max_age_days=90) == 1
    assert cache.load(audio, 4096) is None


def test_prune_on_empty_cache_is_harmless(cache: WaveformCache) -> None:
    assert cache.prune() == 0
