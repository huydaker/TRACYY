"""Lock-free look-ahead buffer between the decoder and the audio callback.

V1 kept decoded PCM as a ``deque`` of ``(start, end, array)`` chunks guarded by
an ``RLock``. Every audio callback took that lock, copied the deque to a tuple
and walked it in Python to find the chunks overlapping the block it needed —
inside the PortAudio callback, while the feeder thread was contending for the
same lock to append the next chunk. Blocking a realtime callback on a lock held
by a normal-priority thread is the classic priority-inversion dropout, and the
per-callback tuple allocation added avoidable garbage on the audio path.

V2 uses a fixed ring indexed by absolute frame number. There is exactly one
producer (the feeder thread) and one consumer (the audio callback), and they
share only two integers. Under CPython those loads and stores are atomic, so
the callback takes no lock, allocates nothing, and does at most two ``memcpy``
slices per block.
"""

from __future__ import annotations

import numpy as np

__all__ = ["PcmRingBuffer"]


class PcmRingBuffer:
    """Stereo float32 ring addressed by absolute frame index.

    The producer writes strictly increasing frame ranges with :meth:`write`.
    The consumer asks for any range with :meth:`read_into`; frames that have
    not been produced yet, or that have already been overwritten, are reported
    as unavailable rather than returning stale audio.
    """

    __slots__ = ("_base_frame", "_capacity", "_channels", "_data", "_write_frame")

    def __init__(self, capacity_frames: int, channels: int = 2) -> None:
        self._capacity = max(1024, int(capacity_frames))
        self._channels = max(1, int(channels))
        self._data = np.zeros((self._capacity, self._channels), dtype=np.float32)
        # Absolute index one past the newest frame written. Producer-owned.
        self._write_frame = 0
        # Absolute index of the oldest frame the ring still holds.
        self._base_frame = 0

    # -- geometry --------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def nbytes(self) -> int:
        return int(self._data.nbytes)

    @property
    def write_frame(self) -> int:
        return self._write_frame

    def reset(self, start_frame: int = 0) -> None:
        """Discard everything and re-anchor at ``start_frame``.

        Only safe while the consumer is stopped — that is, during a seek or a
        stream reopen, which is the only time V2 calls it.
        """
        start_frame = max(0, int(start_frame))
        self._write_frame = start_frame
        self._base_frame = start_frame

    # -- producer --------------------------------------------------------

    def write(self, start_frame: int, block: np.ndarray) -> int:
        """Store ``block`` at absolute ``start_frame``. Returns frames stored.

        Blocks are expected in order. A block that starts before the current
        write cursor is trimmed; one that starts after it leaves a gap, which
        :meth:`read_into` will report as unavailable rather than silently
        playing the wrong audio.
        """
        block = np.asarray(block, dtype=np.float32)
        if block.ndim != 2 or block.shape[1] < self._channels:
            return 0

        start_frame = int(start_frame)
        count = int(block.shape[0])
        if count <= 0:
            return 0

        if start_frame < self._write_frame:
            skip = self._write_frame - start_frame
            if skip >= count:
                return 0
            block = block[skip:]
            start_frame = self._write_frame
            count = int(block.shape[0])
        elif start_frame > self._write_frame:
            # Gap: nothing decoded for the frames in between. Re-anchor so the
            # ring stays contiguous and let the reader see the shortfall.
            self._write_frame = start_frame
            self._base_frame = max(self._base_frame, start_frame - self._capacity)

        if count > self._capacity:
            # Never store more than the ring holds; keep the newest tail.
            block = block[-self._capacity :]
            start_frame = start_frame + count - self._capacity
            count = self._capacity

        offset = start_frame % self._capacity
        first = min(count, self._capacity - offset)
        self._data[offset : offset + first, : self._channels] = block[:first, : self._channels]
        if first < count:
            remainder = count - first
            self._data[:remainder, : self._channels] = block[first:, : self._channels]

        # Publish last: the consumer only ever reads frames below this cursor,
        # so the data is already in place by the time it becomes visible.
        self._write_frame = start_frame + count
        self._base_frame = max(self._base_frame, self._write_frame - self._capacity)
        return count

    # -- consumer --------------------------------------------------------

    def available_from(self, frame: int) -> int:
        """Contiguous frames ready to play from ``frame`` onwards."""
        frame = int(frame)
        write_frame = self._write_frame
        if frame < self._base_frame or frame >= write_frame:
            return 0
        return write_frame - frame

    def read_into(self, destination: np.ndarray, start_frame: int) -> int:
        """Copy up to ``len(destination)`` frames. Returns how many were copied.

        Called from the audio callback: no locks, no allocation, at most two
        slice copies. The destination is *not* cleared — the caller owns that
        decision, and the transport already zeroes its output block.
        """
        wanted = int(destination.shape[0])
        if wanted <= 0:
            return 0

        start_frame = int(start_frame)
        write_frame = self._write_frame
        base_frame = self._base_frame
        if start_frame < base_frame or start_frame >= write_frame:
            return 0

        count = min(wanted, write_frame - start_frame)
        offset = start_frame % self._capacity
        first = min(count, self._capacity - offset)
        destination[:first, : self._channels] = self._data[
            offset : offset + first, : self._channels
        ]
        if first < count:
            remainder = count - first
            destination[first:count, : self._channels] = self._data[
                :remainder, : self._channels
            ]

        # Bounds were checked before the copy; check again after it. The
        # producer can wrap over this region while the memcpy is in flight —
        # rare, but it produces exactly what the class promises never to hand
        # back: half the old block and half the new one. Two integer loads on
        # the callback path buy that guarantee. Reporting a shortfall is the
        # right answer because the transport already treats a short read as
        # "decoder behind" and holds its position instead of skipping.
        if self._base_frame > start_frame:
            return 0

        return count
