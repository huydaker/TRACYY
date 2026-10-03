"""The V2 LTC encoder must be sample-identical to V1's.

V1's implementation is transcribed here verbatim as the reference. If these
tests fail, chase receivers locked to a V1 build would see a different wire
signal, which is the one thing the rewrite is not allowed to change.
"""

from __future__ import annotations

import numpy as np
import pytest

from tracyy.audio.ltc_encoder import LTCGenerator, ltc_frame_bits

# --- V1 reference, copied unchanged from tracyy_audio/transport.py ---------


def _reference_bits(total_frames: int, fps: int) -> list[int]:
    frames_per_100_hours = 100 * 60 * 60 * fps
    total_frames = max(0, int(total_frames)) % frames_per_100_hours

    ff = total_frames % fps
    total_seconds = total_frames // fps
    ss = total_seconds % 60
    total_minutes = total_seconds // 60
    mm = total_minutes % 60
    hh = (total_minutes // 60) % 100

    bits = [0] * 80

    def put(value: int, positions: list[int]) -> None:
        for offset, position in enumerate(positions):
            bits[position] = (value >> offset) & 1

    put(ff % 10, [0, 1, 2, 3])
    put(ff // 10, [8, 9])
    put(ss % 10, [16, 17, 18, 19])
    put(ss // 10, [24, 25, 26])
    put(mm % 10, [32, 33, 34, 35])
    put(mm // 10, [40, 41, 42])
    put(hh % 10, [48, 49, 50, 51])
    put((hh // 10) % 10, [56, 57, 58, 59])

    bits[64:80] = [int(bit) for bit in "0011111111111101"]

    parity_position = 27
    bits[parity_position] = 0
    if bits.count(0) % 2:
        bits[parity_position] = 1

    return bits


class _ReferenceGenerator:
    def __init__(self, sample_rate: float, fps: int, level: float) -> None:
        self.sample_rate = float(sample_rate)
        self.fps = int(fps)
        self.level = float(level)
        self.polarity = 1.0
        self.error = 0.0

    def encode(self, frame_number: int) -> np.ndarray:
        half_bit = self.sample_rate / (self.fps * 160.0)
        samples: list[float] = []

        for bit in _reference_bits(frame_number, self.fps):
            self.polarity *= -1.0

            first_f = half_bit + self.error
            first = max(1, round(first_f))
            self.error = first_f - first
            samples.extend([self.polarity * self.level] * first)

            if bit:
                self.polarity *= -1.0

            second_f = half_bit + self.error
            second = max(1, round(second_f))
            self.error = second_f - second
            samples.extend([self.polarity * self.level] * second)

        expected = round(self.sample_rate / self.fps)
        if len(samples) < expected:
            samples.extend([self.polarity * self.level] * (expected - len(samples)))

        return np.asarray(samples[:expected], dtype=np.float32)


# --- tests ----------------------------------------------------------------


@pytest.mark.parametrize("fps", [24, 25, 30, 50, 60])
@pytest.mark.parametrize("frame", [0, 1, 29, 30, 1234, 99 * 3600 * 30 + 17])
def test_bits_match_v1(fps: int, frame: int) -> None:
    assert ltc_frame_bits(frame, fps) == _reference_bits(frame, fps)


def test_hours_above_23_are_encoded() -> None:
    """The extended range is the point of Tracyy's LTC; guard it explicitly."""
    fps = 30
    frame = ((87 * 60) + 12) * 60 * fps + 5 * fps + 9
    bits = ltc_frame_bits(frame, fps)
    hours_units = sum(bits[48 + i] << i for i in range(4))
    hours_tens = sum(bits[56 + i] << i for i in range(4))
    assert hours_tens * 10 + hours_units == 87


@pytest.mark.parametrize(
    ("sample_rate", "fps"),
    [
        (48000, 30),
        (48000, 25),
        (44100, 30),  # non-integer samples per frame: error carries over
        (44100, 25),
        (96000, 60),
        (44100, 24),
    ],
)
def test_waveform_matches_v1_across_frames(sample_rate: int, fps: int) -> None:
    level = 0.65
    fast = LTCGenerator(sample_rate, fps, level)
    slow = _ReferenceGenerator(sample_rate, fps, level)

    # A full minute: long enough for the carried error to drift apart if the
    # vectorised boundary maths were not equivalent to V1's accumulation.
    for frame in range(fps * 60):
        produced = np.array(fast.encode(frame), copy=True)
        expected = slow.encode(frame)
        assert produced.shape == expected.shape, f"frame {frame}"
        np.testing.assert_array_equal(produced, expected, err_msg=f"frame {frame}")
        assert fast.polarity == pytest.approx(slow.polarity)
        assert fast.error == pytest.approx(slow.error, abs=1e-9)


def test_encode_reuses_its_buffer() -> None:
    generator = LTCGenerator(48000, 30, 0.5)
    first = generator.encode(0)
    second = generator.encode(1)
    assert first is second, "encode() should write into one preallocated buffer"
