"""Disk/codec streaming with a bounded decoded look-ahead window.

Heavy work — demux, decode, sample-rate conversion — runs in a spawned child
process. The parent keeps only a small window of decoded PCM around the
playback position, and the audio callback reads it without taking a lock.

Two things changed from V1:

*Seeking no longer restarts Python.* V1 spawned a fresh decoder process for
every load **and every seek**, and each one paid the cost of importing numpy
and soundfile before it could decode a single sample — a third of a second or
more of silence after every scrub. V2 keeps one decoder process per source and
sends it commands, so a seek is a queue message.

*The callback no longer takes a lock.* V1's ``read_into`` locked the same
``RLock`` the feeder thread used to append chunks, then allocated a tuple and
walked the chunk list in Python — all from the PortAudio callback. V2 reads
from :class:`~tracyy.audio.ringbuffer.PcmRingBuffer`, which needs neither.

This module must stay importable without Qt or sounddevice: the decoder child
imports it.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

from .resampler import StreamResampler

__all__ = ["AudioProbeError", "StreamingAudioSource", "probe_audio"]


class AudioProbeError(RuntimeError):
    pass


def probe_audio(path: str) -> dict[str, object]:
    """Read audio metadata without decoding the whole file."""
    normalized = str(Path(path).resolve())
    try:
        info = sf.info(normalized)
        rate = int(info.samplerate or 0)
        frames = int(info.frames or 0)
        if rate > 0 and frames >= 0:
            return {
                "path": normalized,
                "sample_rate": rate,
                "frames": frames,
                "duration": frames / float(rate),
                "channels": int(info.channels or 0),
                "decoder": "soundfile",
            }
    except (RuntimeError, OSError, sf.LibsndfileError):
        pass

    try:
        import av

        container = av.open(normalized)
        try:
            stream = next(
                (item for item in container.streams if item.type == "audio"),
                None,
            )
            if stream is None:
                raise AudioProbeError("File không có audio stream")
            rate = int(getattr(stream, "rate", 0) or 0) or 48000

            duration = 0.0
            if stream.duration is not None and stream.time_base is not None:
                duration = float(stream.duration * stream.time_base)
            elif container.duration is not None:
                duration = float(container.duration) / 1_000_000.0

            return {
                "path": normalized,
                "sample_rate": rate,
                "frames": max(0, round(duration * rate)),
                "duration": max(0.0, duration),
                "channels": int(getattr(stream, "channels", 0) or 0),
                "decoder": "pyav",
            }
        finally:
            container.close()
    except Exception as exc:
        raise AudioProbeError(
            f"Không đọc được metadata audio: {Path(normalized).name}\n{exc}"
        ) from exc


def _normalize_stereo(block: np.ndarray) -> np.ndarray:
    pcm = np.asarray(block, dtype=np.float32)
    if pcm.ndim != 2:
        pcm = pcm.reshape(-1, 1)
    if pcm.shape[1] == 1:
        pcm = np.repeat(pcm, 2, axis=1)
    elif pcm.shape[1] > 2:
        pcm = pcm[:, :2]
    return np.ascontiguousarray(pcm, dtype=np.float32)


# ---------------------------------------------------------------------------
# Decoder child process
# ---------------------------------------------------------------------------


class _Cancelled(Exception):
    """A newer command arrived; abandon the current decode."""


#: Distinct from ``None``, which is the decoder's quit sentinel.
_NO_COMMAND = object()


def _decode_soundfile(
    path: str,
    target_rate: int,
    start_seconds: float,
    chunk_frames: int,
    emit: Callable[[int, np.ndarray], None],
    cancelled: Callable[[], bool],
) -> int:
    with sf.SoundFile(path, mode="r") as source:
        source_rate = int(source.samplerate)
        resampler = StreamResampler(source_rate, int(target_rate))
        source.seek(min(len(source), max(0, int(start_seconds * source_rate))))

        next_frame = max(0, round(start_seconds * target_rate))
        pending = np.zeros((0, 2), dtype=np.float32)
        read_size = max(4096, int(source_rate))

        def drain(force: bool) -> None:
            nonlocal pending, next_frame
            while len(pending) >= chunk_frames or (force and len(pending)):
                take = min(chunk_frames, len(pending))
                emit(next_frame, np.ascontiguousarray(pending[:take]))
                pending = pending[take:]
                next_frame += take

        while True:
            if cancelled():
                raise _Cancelled
            raw = source.read(read_size, dtype="float32", always_2d=True)
            if raw is None or len(raw) == 0:
                break
            converted = resampler.process(_normalize_stereo(raw))
            if len(converted):
                pending = (
                    converted
                    if not len(pending)
                    else np.concatenate((pending, converted), axis=0)
                )
                drain(False)

        tail = resampler.flush()
        if len(tail):
            pending = tail if not len(pending) else np.concatenate((pending, tail), axis=0)
        drain(True)
        return next_frame


def _decode_pyav(
    path: str,
    target_rate: int,
    start_seconds: float,
    chunk_frames: int,
    emit: Callable[[int, np.ndarray], None],
    cancelled: Callable[[], bool],
) -> int:
    """Fallback streaming decoder for formats libsndfile cannot open."""
    import av

    container = av.open(path)
    try:
        stream = next((item for item in container.streams if item.type == "audio"), None)
        if stream is None:
            raise RuntimeError("File không có audio stream")
        # Let PyAV use several decode threads for the formats that support it.
        stream.thread_type = "AUTO"

        if start_seconds > 0.0:
            # No stream argument => AV_TIME_BASE (microseconds). The seek lands
            # on a key packet at or before the target; the surplus samples are
            # trimmed from the first decoded frame below.
            container.seek(
                max(0, int(start_seconds * 1_000_000.0)),
                backward=True,
                any_frame=False,
            )

        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=int(target_rate))
        requested_start = max(0, round(start_seconds * target_rate))
        next_frame = requested_start
        pending = np.zeros((0, 2), dtype=np.float32)
        first_output = True

        def drain(force: bool) -> None:
            nonlocal pending, next_frame
            while len(pending) >= chunk_frames or (force and len(pending)):
                take = min(chunk_frames, len(pending))
                emit(next_frame, np.ascontiguousarray(pending[:take]))
                pending = pending[take:]
                next_frame += take

        def to_stereo(converted_frame) -> np.ndarray:
            array = converted_frame.to_ndarray()
            # fltp is planar: channels x samples.
            if array.ndim == 2 and array.shape[0] <= 8:
                array = array.T
            return _normalize_stereo(array)

        for frame in container.decode(stream):
            if cancelled():
                raise _Cancelled
            converted = resampler.resample(frame)
            if converted is None:
                continue
            for converted_frame in converted if isinstance(converted, list) else [converted]:
                pcm = to_stereo(converted_frame)

                if first_output:
                    first_output = False
                    frame_time = frame.time
                    if frame_time is not None:
                        frame_start = round(float(frame_time) * target_rate)
                        skip = max(0, requested_start - frame_start)
                        if skip >= len(pcm):
                            continue
                        if skip:
                            pcm = pcm[skip:]

                if len(pcm):
                    pending = (
                        pcm if not len(pending) else np.concatenate((pending, pcm), axis=0)
                    )
                    drain(False)

        flushed = resampler.resample(None)
        if flushed:
            for converted_frame in flushed if isinstance(flushed, list) else [flushed]:
                pcm = to_stereo(converted_frame)
                if len(pcm):
                    pending = (
                        pcm if not len(pending) else np.concatenate((pending, pcm), axis=0)
                    )
        drain(True)
        return next_frame
    finally:
        container.close()


def _decoder_process_main(command_queue, output_queue, stop_event) -> None:
    """Long-lived decoder worker.

    Stays alive across seeks and track changes so the interpreter, numpy and
    the codec libraries are imported once per source rather than once per
    scrub. Messages carry a generation so the parent can discard anything
    produced for a command it has already superseded.
    """
    try:  # pragma: no cover - best effort, platform dependent
        os.nice(5)
    except (AttributeError, OSError, PermissionError):
        pass

    pending_command: object = _NO_COMMAND

    while not stop_event.is_set():
        if pending_command is not _NO_COMMAND:
            command, pending_command = pending_command, _NO_COMMAND
        else:
            try:
                command = command_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                return
        if command is None:
            return

        generation, path, decoder, target_rate, start_seconds, chunk_frames = command

        def cancelled() -> bool:
            """True once a newer command is waiting, so decoding can stop.

            The newer command is held in ``pending_command`` and handled by the
            next turn of the outer loop — including the ``None`` quit sentinel,
            which must not be mistaken for "nothing pending".
            """
            nonlocal pending_command
            if stop_event.is_set():
                return True
            if pending_command is _NO_COMMAND:
                try:
                    pending_command = command_queue.get_nowait()
                except queue.Empty:
                    return False
                except (EOFError, OSError):
                    return True
            return True

        def emit(frame: int, block: np.ndarray, generation=generation) -> None:
            while not stop_event.is_set():
                try:
                    output_queue.put(("chunk", generation, int(frame), block), timeout=0.20)
                    return
                except queue.Full as exc:
                    if cancelled():
                        raise _Cancelled from exc
            raise _Cancelled

        try:
            decode = _decode_soundfile if decoder == "soundfile" else _decode_pyav
            decoded_end = decode(
                path,
                int(target_rate),
                float(start_seconds),
                int(chunk_frames),
                emit,
                cancelled,
            )
        except _Cancelled:
            continue
        except Exception as exc:
            try:
                output_queue.put(("error", generation, 0, str(exc)), timeout=0.25)
            except (queue.Full, OSError):
                pass
            continue

        # EOF must arrive. The parent only reports a track as ended once it
        # has seen this message, so dropping it on a momentarily full queue
        # leaves the transport running forever on a track that has already
        # stopped producing audio. Retry on the same terms as ``emit``: give
        # up only when the work is cancelled or the pipe is gone.
        while not stop_event.is_set():
            try:
                output_queue.put(("eof", generation, int(decoded_end), None), timeout=0.20)
                break
            except queue.Full:
                if cancelled():
                    break
            except OSError:
                break


# ---------------------------------------------------------------------------
# Parent-side source
# ---------------------------------------------------------------------------


class StreamingAudioSource:
    """One track, decoded on demand into a bounded look-ahead ring."""

    def __init__(
        self,
        path: str,
        *,
        buffer_seconds: float = 30.0,
        startup_seconds: float = 5.0,
        keep_behind_seconds: float = 5.0,
        chunk_seconds: float = 1.0,
        on_ready: Callable[[str], None] | None = None,
        on_state: Callable[[str, str, float], None] | None = None,
    ) -> None:
        self.path = str(Path(path).resolve())
        self.probe = probe_audio(self.path)
        self.source_rate = int(self.probe["sample_rate"])
        self.duration = float(self.probe["duration"])
        self.decoder = str(self.probe.get("decoder") or "soundfile")

        self.buffer_seconds = max(8.0, float(buffer_seconds))
        self.startup_seconds = max(1.0, min(float(startup_seconds), self.buffer_seconds * 0.5))
        self.keep_behind_seconds = max(1.0, float(keep_behind_seconds))
        self.chunk_seconds = max(0.25, min(4.0, float(chunk_seconds)))
        self.on_ready = on_ready
        self.on_state = on_state

        self.output_rate = self.source_rate
        self.total_frames = max(0, round(self.duration * self.output_rate))

        # Shared with the audio callback. Plain attributes on purpose: under
        # CPython these loads and stores are atomic, and the callback must not
        # block on a mutex the feeder thread can hold.
        self._ring = self._new_ring(self.output_rate)
        self._position_frame = 0
        self._ready = False
        self._eof = False
        self._error = ""

        # Parent-thread state, guarded only against the feeder thread.
        self._lock = threading.RLock()
        self._generation = 0
        self._request_start_frame = 0

        self._context = mp.get_context("spawn")
        self._command_queue = None
        self._output_queue = None
        self._stop_event = None
        self._process = None
        self._feeder_thread: threading.Thread | None = None

    # -- geometry --------------------------------------------------------

    def _ring_capacity(self, rate: int) -> int:
        span = self.buffer_seconds + self.keep_behind_seconds + self.chunk_seconds * 2
        return max(1 << 15, round(span * max(1, rate)))

    def _new_ring(self, rate: int):
        from .ringbuffer import PcmRingBuffer

        return PcmRingBuffer(self._ring_capacity(rate))

    @property
    def nbytes(self) -> int:
        return int(self._ring.nbytes)

    def __len__(self) -> int:
        return int(self.total_frames)

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._shutdown_process()
        self._ready = False
        self._eof = False

    def _shutdown_process(self) -> None:
        stop_event = self._stop_event
        process = self._process
        command_queue = self._command_queue
        output_queue = self._output_queue

        self._process = None
        self._command_queue = None
        self._output_queue = None
        self._stop_event = None

        if stop_event is not None:
            try:
                stop_event.set()
            except OSError:
                pass
        if command_queue is not None:
            try:
                command_queue.put_nowait(None)
            except (queue.Full, OSError, ValueError):
                pass

        def reap() -> None:
            if process is not None:
                try:
                    process.join(timeout=0.5)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=0.25)
                except (OSError, ValueError):
                    pass
            for item in (command_queue, output_queue):
                if item is None:
                    continue
                try:
                    item.close()
                    item.cancel_join_thread()
                except (OSError, ValueError, AttributeError):
                    pass

        # Never make a caller wait on process teardown; nothing here has to
        # finish before a new source starts.
        threading.Thread(target=reap, name="TracyyStreamReaper", daemon=True).start()

    def _ensure_process(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        self._shutdown_process()

        self._output_queue = self._context.Queue(maxsize=8)
        self._command_queue = self._context.Queue(maxsize=8)
        self._stop_event = self._context.Event()
        self._process = self._context.Process(
            target=_decoder_process_main,
            args=(self._command_queue, self._output_queue, self._stop_event),
            name="TracyyStreamDecoder",
            daemon=True,
        )
        self._process.start()

        self._feeder_thread = threading.Thread(
            target=self._feeder_loop,
            args=(self._output_queue, self._stop_event),
            name="TracyyStreamFeeder",
            daemon=True,
        )
        self._feeder_thread.start()

    def start(self, output_rate: int | None = None, start_seconds: float = 0.0) -> None:
        """(Re)position the decoder. Cheap enough to call on every seek."""
        requested_rate = max(8000, int(output_rate or self.source_rate))
        requested_start = max(0.0, min(self.duration, float(start_seconds)))
        requested_frame = round(requested_start * requested_rate)

        with self._lock:
            # Skip the restart only when the decoder is already positioned here
            # AND the ring still holds this frame. Matching the requested start
            # alone is not enough: _request_start_frame records where the
            # decoder was last *told* to begin, and playback then streams
            # forward for minutes without touching it. Back-to-start on a track
            # played straight through therefore asked for frame 0, matched the
            # 0 it was started at, and returned without doing anything — while
            # the ring had long since scrolled past 0, so read_into could never
            # serve it and _ready was already True so no readiness callback
            # would ever fire again. The transport waited for a buffer that
            # nothing was filling, and the track simply never played.
            if (
                self._process is not None
                and self._process.is_alive()
                and self.output_rate == requested_rate
                and abs(self._request_start_frame - requested_frame) <= 1
                and self._ring.available_from(requested_frame) > 0
            ):
                return

            rate_changed = self.output_rate != requested_rate
            self._generation += 1
            generation = self._generation
            self.output_rate = requested_rate
            self.total_frames = max(0, round(self.duration * requested_rate))
            if rate_changed:
                self._ring = self._new_ring(requested_rate)
            self._ring.reset(requested_frame)
            self._position_frame = requested_frame
            self._request_start_frame = requested_frame
            self._ready = False
            self._eof = False
            self._error = ""

        self._ensure_process()
        chunk_frames = max(1024, round(self.chunk_seconds * requested_rate))
        try:
            self._command_queue.put(
                (
                    generation,
                    self.path,
                    self.decoder,
                    requested_rate,
                    requested_start,
                    chunk_frames,
                ),
                timeout=0.5,
            )
        except (queue.Full, OSError, AttributeError):
            self._error = "Decoder không nhận được lệnh phát."
        self._emit_state("BUFFERING", 0.0)

    def seek(self, seconds: float) -> None:
        self.start(self.output_rate, seconds)

    # -- status ----------------------------------------------------------

    def notify_position(self, frame: int) -> None:
        self._position_frame = max(0, int(frame))

    def ready(self, frame: int | None = None) -> bool:
        if self._error:
            return False
        target = self._position_frame if frame is None else max(0, int(frame))
        ahead = self._ring.available_from(target)
        startup_frames = max(1, round(self.startup_seconds * self.output_rate))
        return bool(
            ahead >= startup_frames or (self._eof and ahead > 0) or target >= self.total_frames
        )

    def buffered_seconds_ahead(self, frame: int | None = None) -> float:
        target = self._position_frame if frame is None else max(0, int(frame))
        return self._ring.available_from(target) / float(max(1, self.output_rate))

    def error_message(self) -> str:
        return self._error

    def _emit_state(self, state: str, ahead_seconds: float) -> None:
        callback = self.on_state
        if callback is None:
            return
        try:
            callback(self.path, str(state), float(ahead_seconds))
        except Exception:
            pass

    # -- feeder ----------------------------------------------------------

    def _feeder_loop(self, output_queue, stop_event) -> None:
        while not stop_event.is_set():
            position = self._position_frame
            rate = self.output_rate
            max_ahead_frames = round(self.buffer_seconds * rate)

            # Backpressure: never let parent RAM grow towards the whole song.
            if self._ring.available_from(position) >= max_ahead_frames:
                time.sleep(0.02)
                continue

            try:
                kind, generation, start, payload = output_queue.get(timeout=0.20)
            except queue.Empty:
                continue
            except (OSError, ValueError, EOFError):
                return

            if generation != self._generation:
                # Produced for a superseded command; the ring must not take it.
                continue

            if kind == "chunk":
                self._accept_chunk(int(start), payload)
            elif kind == "eof":
                self._accept_eof(int(start))
            elif kind == "error":
                self._error = str(payload or "Decoder error")
                self._emit_state("ERROR", 0.0)

    def _accept_chunk(self, start: int, payload) -> None:
        chunk = np.asarray(payload, dtype=np.float32)
        if chunk.ndim != 2 or chunk.shape[1] != 2:
            chunk = _normalize_stereo(chunk)
        self._ring.write(start, chunk)

        ahead_seconds = self.buffered_seconds_ahead()
        became_ready = False
        if not self._ready and self.ready():
            self._ready = True
            became_ready = True
        self._emit_state("READY" if became_ready else "BUFFERING", ahead_seconds)
        if became_ready and self.on_ready is not None:
            try:
                self.on_ready(self.path)
            except Exception:
                pass

    def _accept_eof(self, decoded_end: int) -> None:
        self._eof = True
        if decoded_end > 0:
            self.total_frames = decoded_end
            self.duration = decoded_end / float(max(1, self.output_rate))
        ahead_seconds = self.buffered_seconds_ahead()
        became_ready = False
        if not self._ready and ahead_seconds > 0.0:
            self._ready = True
            became_ready = True
        self._emit_state("READY" if became_ready else "EOF", ahead_seconds)
        if became_ready and self.on_ready is not None:
            try:
                self.on_ready(self.path)
            except Exception:
                pass

    # -- audio callback --------------------------------------------------

    def read_into(self, destination: np.ndarray, start_frame: int) -> tuple[int, bool]:
        """Copy buffered stereo PCM into the callback's scratch buffer.

        Realtime path: no locks, no allocation, at most two ``memcpy`` slices.
        """
        if destination.ndim != 2 or destination.shape[1] < 2:
            raise ValueError("destination phải là float32 stereo buffer")
        if destination.shape[0] <= 0:
            return 0, False

        start_frame = max(0, int(start_frame))
        copied = self._ring.read_into(destination, start_frame)
        if copied < destination.shape[0]:
            destination[copied:, :2] = 0.0
        ended = bool(self._eof and start_frame + copied >= self.total_frames)
        return int(copied), ended
