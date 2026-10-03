"""SMPTE-style LTC generation, vectorised.

Tracyy uses an extended LTC: the hours field is encoded 00-99 instead of the
standard 00-23, using all four bits of the tens-of-hours nibble, with parity
moved to bit 27 for every frame rate. That wire format is unchanged from V1 —
existing shows and existing chase receivers must keep decoding it.

What changed is how the samples are produced. V1 built each frame by appending
to a Python list, one element per output sample: at 48 kHz/30 fps that is 1600
list appends and a fresh list allocation per frame, executed *inside the
PortAudio callback*. V2 computes the same waveform with three numpy
operations into a buffer that is allocated once.

The biphase-mark encoding, the error-diffused half-bit lengths and the
polarity carried across frames are all bit-identical to V1; only the arithmetic
is done differently. ``_reference_encode`` in the tests pins that down.
"""

from __future__ import annotations

import numpy as np

__all__ = ["SYNC_WORD", "LTCGenerator", "ltc_frame_bits"]

#: Trailing sync word, LSB-first as written into bits 64..79.
SYNC_WORD = "0011111111111101"
_SYNC_BITS = np.frombuffer(SYNC_WORD.encode("ascii"), dtype=np.uint8) - ord("0")

#: Bit positions for each BCD field, least-significant bit first.
_FIELDS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("frames_units", (0, 1, 2, 3)),
    ("frames_tens", (8, 9)),
    ("seconds_units", (16, 17, 18, 19)),
    ("seconds_tens", (24, 25, 26)),
    ("minutes_units", (32, 33, 34, 35)),
    ("minutes_tens", (40, 41, 42)),
    ("hours_units", (48, 49, 50, 51)),
    # All four bits, so hours can reach 99. Bit 59 is therefore *not*
    # available as the classic parity bit.
    ("hours_tens", (56, 57, 58, 59)),
)

#: Parity lives at bit 27 in extended mode, at every frame rate.
_PARITY_POSITION = 27

_FRAMES_PER_100_HOURS_CACHE: dict[int, int] = {}


def _split_timecode(total_frames: int, fps: int) -> tuple[int, int, int, int]:
    wrap = _FRAMES_PER_100_HOURS_CACHE.get(fps)
    if wrap is None:
        wrap = 100 * 60 * 60 * fps
        _FRAMES_PER_100_HOURS_CACHE[fps] = wrap

    total_frames = max(0, int(total_frames)) % wrap
    ff = total_frames % fps
    total_seconds = total_frames // fps
    ss = total_seconds % 60
    total_minutes = total_seconds // 60
    mm = total_minutes % 60
    hh = (total_minutes // 60) % 100
    return hh, mm, ss, ff


def frame_bits_array(total_frames: int, fps: int, out: np.ndarray | None = None) -> np.ndarray:
    """The 80 LTC bits for ``total_frames``, as ``uint8`` 0/1."""
    bits = np.zeros(80, dtype=np.uint8) if out is None else out
    bits[:] = 0

    hh, mm, ss, ff = _split_timecode(total_frames, fps)
    values = (
        ff % 10,
        ff // 10,
        ss % 10,
        ss // 10,
        mm % 10,
        mm // 10,
        hh % 10,
        (hh // 10) % 10,
    )
    for value, (_name, positions) in zip(values, _FIELDS, strict=True):
        for offset, position in enumerate(positions):
            bits[position] = (value >> offset) & 1

    bits[64:80] = _SYNC_BITS

    # Even parity over the *zero* bits, matching V1 exactly.
    if int(np.count_nonzero(bits == 0)) % 2:
        bits[_PARITY_POSITION] = 1
    return bits


def ltc_frame_bits(total_frames: int, fps: int) -> list[int]:
    """List form of :func:`frame_bits_array`, kept for the V1 call sites."""
    return frame_bits_array(total_frames, fps).tolist()


class LTCGenerator:
    """Streaming biphase-mark encoder for one output device.

    One generator instance belongs to one device's audio callback; it is not
    thread-safe and does not need to be. ``polarity`` and ``error`` carry
    across frames so consecutive frames join seamlessly, exactly as in V1.
    """

    __slots__ = (
        "_bits",
        "_expected",
        "_flips",
        "_half_bit",
        "_length_cache",
        "_lengths",
        "_out",
        "_segment_index",
        "error",
        "fps",
        "level",
        "polarity",
        "sample_rate",
    )

    def __init__(self, sample_rate: float, fps: int, level: float) -> None:
        self.sample_rate = float(sample_rate)
        self.fps = int(fps)
        self.level = float(level)
        self.polarity = 1.0
        self.error = 0.0

        self._half_bit = self.sample_rate / (self.fps * 160.0)
        self._expected = round(self.sample_rate / self.fps)
        self._bits = np.zeros(80, dtype=np.uint8)
        self._flips = np.empty(160, dtype=np.int8)
        self._out = np.zeros(self._expected, dtype=np.float32)
        self._length_cache: dict[float, tuple[np.ndarray, float]] = {}
        # Half-bit ordinals 1..160 for the vectorised boundary computation.
        self._segment_index = np.arange(1, 161, dtype=np.float64)
        self._lengths = np.empty(160, dtype=np.int64)

    def _segment_lengths(self) -> tuple[np.ndarray, float]:
        """Sample count for each of the 160 half-bits of the next frame.

        The lengths depend only on the half-bit width and the error carried in
        from the previous frame — never on the timecode.

        Error-diffused rounding is a rounded *cumulative* position, so the
        whole frame is normally one vectorised ``rint``. The one case where
        that is not equivalent is an exact ``.5`` boundary: round-half-to-even
        picks a different neighbour depending on the running total, which only
        the sequential loop knows. Exact ties need a half-bit width that is a
        dyadic rational (48 kHz at 30 fps, 44.1 kHz at 30 fps, …), and those
        widths make the carried error settle into one or two values within a
        frame or two — so the slow path is taken a handful of times per
        session and then served from the cache.
        """
        cached = self._length_cache.get(self.error)
        if cached is not None:
            return cached

        positions = self._segment_index * self._half_bit + self.error
        boundaries = np.rint(positions)
        if np.any(positions - np.floor(positions) == 0.5):
            return self._sequential_lengths()

        lengths = self._lengths
        lengths[0] = boundaries[0]
        np.subtract(boundaries[1:], boundaries[:-1], out=lengths[1:], casting="unsafe")
        # V1 guaranteed at least one sample per half-bit. Only reachable at
        # absurdly low sample rates, but the guarantee is kept.
        np.maximum(lengths, 1, out=lengths)
        return lengths, float(positions[-1] - boundaries[-1])

    def _sequential_lengths(self) -> tuple[np.ndarray, float]:
        half_bit = self._half_bit
        error = self.error
        lengths = np.empty(160, dtype=np.int64)
        for index in range(160):
            width = half_bit + error
            count = max(1, round(width))
            error = width - count
            lengths[index] = count

        if len(self._length_cache) > 256:  # pragma: no cover - pathological
            self._length_cache.clear()
        entry = (lengths, error)
        self._length_cache[self.error] = entry
        return entry

    def encode(self, frame_number: int) -> np.ndarray:
        """One frame of LTC audio.

        The returned array is a view onto an internal buffer that the next
        call overwrites. Callers that keep it must copy — the transport reads
        it straight into the output block, so it does not.
        """
        bits = frame_bits_array(frame_number, self.fps, self._bits)
        lengths, error_after = self._segment_lengths()

        # Biphase mark: flip at every bit boundary, and again mid-bit for a 1.
        flips = self._flips
        flips[0::2] = 1
        flips[1::2] = bits

        # Running polarity = start polarity, sign-flipped once per flip.
        signs = np.where(np.cumsum(flips) % 2 == 0, 1.0, -1.0) * self.polarity
        samples = np.repeat(signs.astype(np.float32), lengths)

        self.polarity = float(signs[-1])
        self.error = error_after

        expected = self._expected
        out = self._out
        if len(out) != expected:
            out = self._out = np.zeros(expected, dtype=np.float32)

        count = min(expected, samples.size)
        np.multiply(samples[:count], np.float32(self.level), out=out[:count])
        if count < expected:
            # Pad with the trailing polarity, as V1 did.
            out[count:] = np.float32(self.polarity * self.level)
        return out
