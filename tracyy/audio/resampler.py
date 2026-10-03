"""Sample-rate conversion for the streaming decoder.

V1 resampled with its own linear interpolator. Linear interpolation is cheap
but it is also a poor reconstruction filter: it attenuates the top of the band
and folds an image of everything above Nyquist back down, which is audible as
a dull, slightly gritty top end whenever a 44.1 kHz track is played out of a
48 kHz interface — the single most common configuration on a show machine.

V2 uses `soxr <https://github.com/dofuuz/python-soxr>`_, a band-limited
polyphase resampler, and keeps the V1 interpolator as the fallback for builds
where soxr is unavailable. Both run in the decoder child process, never in an
audio callback.
"""

from __future__ import annotations

import os

import numpy as np

__all__ = [
    "SOXR_AVAILABLE",
    "StreamResampler",
    "resample_block",
    "resampler_name",
]

try:
    import soxr

    SOXR_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the build environment
    soxr = None  # type: ignore[assignment]
    SOXR_AVAILABLE = False


def _quality() -> str:
    """soxr preset. ``HQ`` is transparent and comfortably realtime."""
    raw = str(os.environ.get("TRACYY_RESAMPLE_QUALITY", "")).strip().upper()
    return raw if raw in {"QQ", "LQ", "MQ", "HQ", "VHQ"} else "HQ"


def _forced_linear() -> bool:
    return str(os.environ.get("TRACYY_LINEAR_RESAMPLE", "")).strip() in {"1", "true", "TRUE"}


def resampler_name() -> str:
    if SOXR_AVAILABLE and not _forced_linear():
        return f"soxr {_quality()}"
    return "linear (fallback)"


class StreamResampler:
    """Stateful stereo resampler for a sequential decode.

    Feed it decoded blocks in order and it returns the corresponding output
    blocks; the filter state carries across calls, so block boundaries are
    seamless and no timing drift accumulates. ``flush`` drains the filter tail
    at end of file.
    """

    def __init__(self, source_rate: int, target_rate: int, channels: int = 2) -> None:
        self.source_rate = int(source_rate)
        self.target_rate = int(target_rate)
        self.channels = int(channels)
        self.passthrough = self.source_rate == self.target_rate
        self._stream = None
        self._ratio = self.target_rate / float(self.source_rate)
        # Fallback interpolator state: retained source samples, the absolute
        # source index they start at, and how many output frames were emitted.
        self._buffer = np.zeros((0, self.channels), dtype=np.float32)
        self._buffer_start = 0
        self._emitted = 0

        if self.passthrough:
            return
        if SOXR_AVAILABLE and not _forced_linear():
            self._stream = soxr.ResampleStream(
                float(self.source_rate),
                float(self.target_rate),
                self.channels,
                dtype="float32",
                quality=_quality(),
            )

    @property
    def backend(self) -> str:
        if self.passthrough:
            return "passthrough"
        return "soxr" if self._stream is not None else "linear"

    def process(self, block: np.ndarray, *, last: bool = False) -> np.ndarray:
        """Resample one block. Output length varies; that is expected."""
        block = np.ascontiguousarray(block, dtype=np.float32)
        if self.passthrough:
            return block
        if self._stream is not None:
            out = self._stream.resample_chunk(block, last=bool(last))
            return np.ascontiguousarray(out, dtype=np.float32)
        return self._linear_process(block, last=last)

    def flush(self) -> np.ndarray:
        """Remaining output after the last input block."""
        if self.passthrough:
            return np.zeros((0, self.channels), dtype=np.float32)
        if self._stream is not None:
            empty = np.zeros((0, self.channels), dtype=np.float32)
            return np.ascontiguousarray(
                self._stream.resample_chunk(empty, last=True),
                dtype=np.float32,
            )
        # Hold the final source sample so the tail of the last block is
        # emitted instead of being cut at the last interpolatable point.
        if len(self._buffer):
            self._buffer = np.concatenate(
                (self._buffer, self._buffer[-1:]),
                axis=0,
            )
        return self._linear_process(
            np.zeros((0, self.channels), dtype=np.float32),
            last=True,
        )

    # -- fallback --------------------------------------------------------

    def _linear_process(self, block: np.ndarray, *, last: bool) -> np.ndarray:
        """V1's interpolator, made block-stateful.

        Output positions are absolute source positions, so block boundaries
        cannot accumulate drift, and the samples straddling a boundary are
        retained so a point falling across the join is interpolated rather
        than clamped to the block edge.
        """
        empty = np.zeros((0, self.channels), dtype=np.float32)

        if len(block):
            self._buffer = (
                np.concatenate((self._buffer, block), axis=0)
                if len(self._buffer)
                else np.ascontiguousarray(block, dtype=np.float32)
            )
        buffer = self._buffer
        if len(buffer) < 2:
            return empty

        buffer_start = self._buffer_start
        # Highest source position that can still be interpolated from what is
        # buffered. At end of input the final sample is held instead.
        reachable = buffer_start + len(buffer) - 1
        end = int(np.floor(reachable * self._ratio)) + 1
        if end <= self._emitted:
            return empty

        targets = np.arange(self._emitted, end, dtype=np.float64)
        positions = targets / self._ratio - buffer_start
        np.clip(positions, 0.0, len(buffer) - 1.0, out=positions)
        left = np.floor(positions).astype(np.int64)
        right = np.minimum(left + 1, len(buffer) - 1)
        fraction = (positions - left).astype(np.float32)
        inverse = (1.0 - fraction).astype(np.float32)

        out = np.empty((len(targets), self.channels), dtype=np.float32)
        for channel in range(self.channels):
            out[:, channel] = (
                buffer[left, channel] * inverse + buffer[right, channel] * fraction
            )
        self._emitted = end

        # Drop source samples no future output point can reach, keeping one
        # behind the next needed position for the interpolation pair.
        next_source = int(np.floor(self._emitted / self._ratio)) - 1
        discard = max(0, min(len(buffer) - 2, next_source - buffer_start))
        if discard:
            self._buffer = np.ascontiguousarray(buffer[discard:], dtype=np.float32)
            self._buffer_start = buffer_start + discard
        return out


def resample_block(block: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """One-shot conversion of a complete array. Offline analysis only."""
    block = np.ascontiguousarray(block, dtype=np.float32)
    if int(source_rate) == int(target_rate) or len(block) == 0:
        return block
    if SOXR_AVAILABLE and not _forced_linear():
        return np.ascontiguousarray(
            soxr.resample(block, float(source_rate), float(target_rate), quality=_quality()),
            dtype=np.float32,
        )
    channels = 1 if block.ndim == 1 else block.shape[1]
    stream = StreamResampler(source_rate, target_rate, channels)
    head = stream.process(block, last=True)
    tail = stream.flush()
    return np.concatenate((head, tail), axis=0) if len(tail) else head
