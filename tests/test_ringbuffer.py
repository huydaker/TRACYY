"""Ring buffer behaviour, including the concurrent producer/consumer case."""

from __future__ import annotations

import threading

import numpy as np

from tracyy.audio.ringbuffer import PcmRingBuffer


def _ramp(start: int, count: int) -> np.ndarray:
    values = np.arange(start, start + count, dtype=np.float32)
    return np.stack([values, values + 0.5], axis=1)


def test_sequential_write_and_read() -> None:
    ring = PcmRingBuffer(4096)
    ring.write(0, _ramp(0, 1000))
    ring.write(1000, _ramp(1000, 1000))

    destination = np.zeros((1500, 2), dtype=np.float32)
    copied = ring.read_into(destination, 250)
    assert copied == 1500
    np.testing.assert_array_equal(destination, _ramp(250, 1500))


def test_wraparound_is_contiguous() -> None:
    ring = PcmRingBuffer(1024)
    for start in range(0, 4096, 300):
        ring.write(start, _ramp(start, 300))

    destination = np.zeros((512, 2), dtype=np.float32)
    newest = ring.write_frame - 512
    assert ring.read_into(destination, newest) == 512
    np.testing.assert_array_equal(destination, _ramp(newest, 512))


def test_overwritten_frames_are_not_returned() -> None:
    ring = PcmRingBuffer(1024)
    ring.write(0, _ramp(0, 4096))
    destination = np.zeros((64, 2), dtype=np.float32)
    # Frame 0 fell out of the ring long ago; it must not come back as stale
    # audio just because the index still maps into the array.
    assert ring.read_into(destination, 0) == 0
    assert ring.available_from(0) == 0


def test_unwritten_frames_are_not_returned() -> None:
    ring = PcmRingBuffer(4096)
    ring.write(0, _ramp(0, 100))
    destination = np.zeros((64, 2), dtype=np.float32)
    assert ring.read_into(destination, 100) == 0
    assert ring.read_into(destination, 50) == 50


def test_short_read_reports_what_it_had() -> None:
    ring = PcmRingBuffer(4096)
    ring.write(0, _ramp(0, 100))
    destination = np.zeros((256, 2), dtype=np.float32)
    assert ring.read_into(destination, 0) == 100
    np.testing.assert_array_equal(destination[:100], _ramp(0, 100))


def test_reset_reanchors() -> None:
    ring = PcmRingBuffer(4096)
    ring.write(0, _ramp(0, 1000))
    ring.reset(50_000)
    destination = np.zeros((32, 2), dtype=np.float32)
    assert ring.read_into(destination, 0) == 0
    ring.write(50_000, _ramp(50_000, 500))
    assert ring.read_into(destination, 50_000) == 32


def test_concurrent_producer_and_consumer_never_tear() -> None:
    """A block handed to the consumer must be internally consistent.

    Every frame carries its own index, so a torn read — half from the block
    being overwritten, half from the new one — is detectable.
    """
    ring = PcmRingBuffer(48000)
    stop = threading.Event()
    failures: list[str] = []

    def produce() -> None:
        frame = 0
        while not stop.is_set() and frame < 48000 * 20:
            ring.write(frame, _ramp(frame, 512))
            frame += 512

    def consume() -> None:
        destination = np.zeros((256, 2), dtype=np.float32)
        cursor = 0
        while not stop.is_set():
            copied = ring.read_into(destination, cursor)
            if copied <= 0:
                if cursor >= 48000 * 20:
                    return
                continue
            expected = _ramp(cursor, copied)
            if not np.array_equal(destination[:copied], expected):
                failures.append(f"torn read at {cursor}")
                return
            cursor += copied

    producer = threading.Thread(target=produce)
    consumer = threading.Thread(target=consume)
    producer.start()
    consumer.start()
    producer.join(timeout=30)
    stop.set()
    consumer.join(timeout=5)

    assert not failures, failures[0]
