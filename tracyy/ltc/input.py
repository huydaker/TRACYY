from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

LTC_SYNC_BITS = tuple(int(bit) for bit in "0011111111111101")
SUPPORTED_LTC_FPS = (24, 25, 30)
LTC_DECODE_PHASE_FRAMES = 1


@dataclass(frozen=True)
class LTCFrame:
    hours: int
    minutes: int
    seconds: int
    frames: int
    fps: int
    total_frames: int
    timecode: str
    received_monotonic: float
    captured_monotonic: float = 0.0
    delivery_latency_seconds: float = 0.0
    source: str = "LTC_AUDIO"


def compensated_total_frames(
    frame: LTCFrame,
    extra_compensation_frames: int = 0,
) -> int:
    """Return the timeline frame represented *now* by an LTC frame.

    LTC is decoded only after its 80-bit word has completed. Therefore a
    forward-running source is normally one frame beyond the label that has
    just been decoded. PortAudio's ADC timestamp is used to add any audio
    buffering delay between capture and callback delivery.
    """

    fps = max(1, int(frame.fps))
    source = str(frame.source or "").upper()
    phase_frames = LTC_DECODE_PHASE_FRAMES if source == "LTC_AUDIO" else 0
    latency_seconds = max(0.0, float(frame.delivery_latency_seconds or 0.0))
    latency_frames = round(latency_seconds * fps)
    day_frames = 24 * 60 * 60 * fps

    return (
        int(frame.total_frames) + phase_frames + latency_frames + int(extra_compensation_frames)
    ) % day_frames


@dataclass(frozen=True)
class LTCValidationResult:
    accepted: bool
    status: str
    raw_total_frames: int
    effective_total_frames: int
    fps: int
    progress: float = 0.0
    candidate_count: int = 0
    required_count: int = 0
    confirmed_seek: bool = False
    signal_present: bool = True


class LTCFrameContinuityValidator:
    """Validate LTC continuity before a frame can control the timeline.

    This layer prevents a single malformed but BCD-valid frame, especially
    00:00:00:00, from resetting the clock, track, playhead and cues.

    Normal forward frames are accepted immediately after the initial pre-roll.
    A large discontinuity is treated as a seek candidate and must be confirmed
    by a short consecutive sequence before it replaces Last Good TC.
    """

    def __init__(
        self,
        fps: int,
        pre_roll_seconds: float = 0.30,
        forward_dropout_tolerance: int = 4,
        backward_jitter_guard: int = 3,
        seek_confirm_frames: int = 4,
        zero_confirm_frames: int = 5,
    ) -> None:
        self.forward_dropout_tolerance = max(
            1,
            int(forward_dropout_tolerance),
        )
        self.backward_jitter_guard = max(
            0,
            int(backward_jitter_guard),
        )
        self.seek_confirm_frames = max(
            2,
            int(seek_confirm_frames),
        )
        self.zero_confirm_frames = max(
            self.seek_confirm_frames,
            int(zero_confirm_frames),
        )

        self.fps = 30
        self.pre_roll_seconds = 0.30
        self.day_frames = 24 * 60 * 60 * 30
        self.locked = False

        self.last_good_raw_frames: int | None = None
        self.last_good_effective_frames: int | None = None
        self.last_good_timestamp = 0.0

        self.preroll_started_at = 0.0
        self.preroll_last_raw_frames: int | None = None
        self.preroll_count = 0

        self.seek_candidate_started_at = 0.0
        self.seek_candidate_last_raw_frames: int | None = None
        self.seek_candidate_effective_frames = 0
        self.seek_candidate_count = 0
        self.seek_candidate_required = self.seek_confirm_frames

        self.configure(
            fps=fps,
            pre_roll_seconds=pre_roll_seconds,
            reset=True,
        )

    def configure(
        self,
        fps: int | None = None,
        pre_roll_seconds: float | None = None,
        reset: bool = False,
    ) -> None:
        new_fps = self.fps if fps is None else int(fps)
        if new_fps not in SUPPORTED_LTC_FPS:
            raise ValueError("fps must be 24, 25 or 30")

        fps_changed = new_fps != self.fps
        self.fps = new_fps
        self.day_frames = 24 * 60 * 60 * self.fps

        if pre_roll_seconds is not None:
            self.pre_roll_seconds = max(
                0.0,
                float(pre_roll_seconds),
            )

        if reset or fps_changed:
            self.reset()

    def reset(self) -> None:
        self.locked = False

        self.last_good_raw_frames = None
        self.last_good_effective_frames = None
        self.last_good_timestamp = 0.0

        self._reset_preroll()
        self._reset_seek_candidate()

    def reset_preroll_only(self) -> None:
        if not self.locked:
            self._reset_preroll()

    def _reset_preroll(self) -> None:
        self.preroll_started_at = 0.0
        self.preroll_last_raw_frames = None
        self.preroll_count = 0

    def _reset_seek_candidate(self) -> None:
        self.seek_candidate_started_at = 0.0
        self.seek_candidate_last_raw_frames = None
        self.seek_candidate_effective_frames = 0
        self.seek_candidate_count = 0
        self.seek_candidate_required = self.seek_confirm_frames

    def _forward_delta(
        self,
        newer: int,
        older: int,
    ) -> int:
        return (int(newer) - int(older)) % self.day_frames

    def _backward_delta(
        self,
        newer: int,
        older: int,
    ) -> int:
        return (int(older) - int(newer)) % self.day_frames

    def _dynamic_forward_limit(
        self,
        timestamp: float,
    ) -> int:
        elapsed = max(
            0.0,
            float(timestamp) - float(self.last_good_timestamp),
        )
        expected = round(elapsed * self.fps)
        return max(
            self.forward_dropout_tolerance,
            expected + 2,
        )

    def _accept(
        self,
        raw_total_frames: int,
        effective_total_frames: int,
        timestamp: float,
        status: str,
        confirmed_seek: bool = False,
    ) -> LTCValidationResult:
        raw = int(raw_total_frames) % self.day_frames
        effective = int(effective_total_frames) % self.day_frames

        self.locked = True
        self.last_good_raw_frames = raw
        self.last_good_effective_frames = effective
        self.last_good_timestamp = float(timestamp)

        self._reset_preroll()
        self._reset_seek_candidate()

        return LTCValidationResult(
            accepted=True,
            status=status,
            raw_total_frames=raw,
            effective_total_frames=effective,
            fps=self.fps,
            progress=1.0,
            confirmed_seek=bool(confirmed_seek),
        )

    def _process_preroll(
        self,
        raw_total_frames: int,
        effective_total_frames: int,
        timestamp: float,
    ) -> LTCValidationResult:
        raw = int(raw_total_frames) % self.day_frames
        effective = int(effective_total_frames) % self.day_frames
        now = float(timestamp)

        if self.preroll_last_raw_frames is None or self.preroll_started_at <= 0.0:
            self.preroll_started_at = now
            self.preroll_last_raw_frames = raw
            self.preroll_count = 1

        else:
            delta = self._forward_delta(
                raw,
                self.preroll_last_raw_frames,
            )

            if delta == 0:
                # A stopped but continuously transmitted LTC frame still
                # represents a stable signal. Do not reset pre-roll.
                pass

            elif 1 <= delta <= self.forward_dropout_tolerance:
                self.preroll_last_raw_frames = raw
                self.preroll_count += 1

            else:
                # Start a fresh candidate sequence from the current frame.
                self.preroll_started_at = now
                self.preroll_last_raw_frames = raw
                self.preroll_count = 1

                return LTCValidationResult(
                    accepted=False,
                    status="PREROLL_RESET",
                    raw_total_frames=raw,
                    effective_total_frames=effective,
                    fps=self.fps,
                    progress=0.0,
                    candidate_count=1,
                    required_count=max(
                        3,
                        round(self.pre_roll_seconds * self.fps * 0.50),
                    ),
                )

        elapsed = max(
            0.0,
            now - self.preroll_started_at,
        )

        required_count = max(
            3,
            round(self.pre_roll_seconds * self.fps * 0.50),
        )

        progress = (
            1.0
            if self.pre_roll_seconds <= 0.0
            else min(
                1.0,
                elapsed / self.pre_roll_seconds,
            )
        )

        enough_time = self.pre_roll_seconds <= 0.0 or elapsed >= self.pre_roll_seconds
        enough_frames = self.preroll_count >= required_count

        if enough_time and enough_frames:
            return self._accept(
                raw,
                effective,
                now,
                status="LOCKED_FROM_PREROLL",
            )

        return LTCValidationResult(
            accepted=False,
            status="PREROLL",
            raw_total_frames=raw,
            effective_total_frames=effective,
            fps=self.fps,
            progress=progress,
            candidate_count=self.preroll_count,
            required_count=required_count,
        )

    def _process_seek_candidate(
        self,
        raw_total_frames: int,
        effective_total_frames: int,
        timestamp: float,
    ) -> LTCValidationResult:
        raw = int(raw_total_frames) % self.day_frames
        effective = int(effective_total_frames) % self.day_frames
        now = float(timestamp)

        zero_like = raw < max(
            self.fps * 2,
            1,
        )
        required = self.zero_confirm_frames if zero_like else self.seek_confirm_frames

        if self.seek_candidate_last_raw_frames is None:
            self.seek_candidate_started_at = now
            self.seek_candidate_last_raw_frames = raw
            self.seek_candidate_effective_frames = effective
            self.seek_candidate_count = 1
            self.seek_candidate_required = required

        else:
            delta = self._forward_delta(
                raw,
                self.seek_candidate_last_raw_frames,
            )

            if delta == 0:
                # Repeated timecode is allowed for a paused source.
                self.seek_candidate_count += 1
                self.seek_candidate_effective_frames = effective

            elif 1 <= delta <= self.forward_dropout_tolerance:
                self.seek_candidate_last_raw_frames = raw
                self.seek_candidate_effective_frames = effective
                self.seek_candidate_count += 1

            else:
                self.seek_candidate_started_at = now
                self.seek_candidate_last_raw_frames = raw
                self.seek_candidate_effective_frames = effective
                self.seek_candidate_count = 1

            self.seek_candidate_required = required

        if self.seek_candidate_count >= self.seek_candidate_required:
            return self._accept(
                raw,
                effective,
                now,
                status="SEEK_CONFIRMED",
                confirmed_seek=True,
            )

        return LTCValidationResult(
            accepted=False,
            status="SEEK_CANDIDATE",
            raw_total_frames=raw,
            effective_total_frames=effective,
            fps=self.fps,
            candidate_count=self.seek_candidate_count,
            required_count=self.seek_candidate_required,
        )

    def process(
        self,
        raw_total_frames: int,
        effective_total_frames: int,
        timestamp: float,
        source: str = "LTC_AUDIO",
    ) -> LTCValidationResult:
        source_name = str(source or "").upper()
        raw = int(raw_total_frames) % self.day_frames
        effective = int(effective_total_frames) % self.day_frames
        now = float(timestamp)

        # Manual test input is an operator command, not an untrusted decoded
        # audio frame, so accept it immediately.
        if source_name == "MANUAL_TEST":
            return self._accept(
                raw,
                effective,
                now,
                status="MANUAL_ACCEPT",
                confirmed_seek=True,
            )

        if not self.locked or self.last_good_raw_frames is None:
            return self._process_preroll(
                raw,
                effective,
                now,
            )

        delta_forward = self._forward_delta(
            raw,
            self.last_good_raw_frames,
        )

        if delta_forward == 0:
            return self._accept(
                raw,
                effective,
                now,
                status="HOLD_FRAME",
            )

        if 1 <= delta_forward <= self._dynamic_forward_limit(now):
            return self._accept(
                raw,
                effective,
                now,
                status="FORWARD",
            )

        delta_backward = self._backward_delta(
            raw,
            self.last_good_raw_frames,
        )

        if 1 <= delta_backward <= self.backward_jitter_guard:
            self._reset_seek_candidate()

            return LTCValidationResult(
                accepted=False,
                status="BACKWARD_JITTER",
                raw_total_frames=raw,
                effective_total_frames=effective,
                fps=self.fps,
            )

        return self._process_seek_candidate(
            raw,
            effective,
            now,
        )


class LTCStreamDecoder:
    """Stateful forward-play LTC decoder for 24, 25 and 30 fps.

    The decoder keeps state across arbitrary callback chunk sizes. When a
    PortAudio ADC timestamp is supplied, each decoded frame is timestamped at
    the sample where its codeword ended instead of at Python callback time.
    """

    def __init__(self, sample_rate: float, fps: int) -> None:
        self.sample_rate = float(sample_rate)
        self.fps = int(fps)
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if self.fps not in SUPPORTED_LTC_FPS:
            raise ValueError("fps must be 24, 25 or 30")

        self.half_bit_samples = self.sample_rate / (self.fps * 160.0)
        self._signal_state = 0
        self._sample_index = 0
        self._last_transition_index: int | None = None
        self._pending_half_interval = False
        self._bits: deque[int] = deque(maxlen=320)
        self._last_total_frames: int | None = None
        self._invalid_transition_count = 0
        self._invalid_transition_reset_threshold = 12

    @staticmethod
    def _read_value(bits: list[int], positions: list[int]) -> int:
        return sum(int(bits[position]) << shift for shift, position in enumerate(positions))

    def _parse_frame(
        self,
        bits: list[int],
        frame_end_sample: int,
        chunk_start_sample: int,
        chunk_start_monotonic: float,
        delivered_monotonic: float,
    ) -> LTCFrame | None:
        if len(bits) != 80 or tuple(bits[64:80]) != LTC_SYNC_BITS:
            return None

        frame_units = self._read_value(bits, [0, 1, 2, 3])
        frame_tens = self._read_value(bits, [8, 9])
        second_units = self._read_value(bits, [16, 17, 18, 19])
        second_tens = self._read_value(bits, [24, 25, 26])
        minute_units = self._read_value(bits, [32, 33, 34, 35])
        minute_tens = self._read_value(bits, [40, 41, 42])
        hour_units = self._read_value(bits, [48, 49, 50, 51])
        hour_tens = self._read_value(bits, [56, 57])

        ff = frame_units + frame_tens * 10
        ss = second_units + second_tens * 10
        mm = minute_units + minute_tens * 10
        hh = hour_units + hour_tens * 10

        if not (0 <= ff < self.fps and 0 <= ss < 60 and 0 <= mm < 60 and 0 <= hh < 24):
            return None

        total_frames = (((hh * 60) + mm) * 60 + ss) * self.fps + ff
        self._last_total_frames = total_frames

        sample_offset = max(0, int(frame_end_sample) - int(chunk_start_sample))
        captured = float(chunk_start_monotonic) + sample_offset / self.sample_rate
        delivered = max(float(delivered_monotonic), captured)
        latency = max(0.0, delivered - captured)

        return LTCFrame(
            hours=hh,
            minutes=mm,
            seconds=ss,
            frames=ff,
            fps=self.fps,
            total_frames=total_frames,
            timecode=f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}",
            received_monotonic=delivered,
            captured_monotonic=captured,
            delivery_latency_seconds=latency,
        )

    def _append_bit(
        self,
        bit: int,
        output: list[LTCFrame],
        frame_end_sample: int,
        chunk_start_sample: int,
        chunk_start_monotonic: float,
        delivered_monotonic: float,
    ) -> None:
        self._bits.append(1 if bit else 0)
        if len(self._bits) < 80:
            return

        current = list(self._bits)
        if tuple(current[-16:]) != LTC_SYNC_BITS:
            return

        decoded = self._parse_frame(
            current[-80:],
            frame_end_sample,
            chunk_start_sample,
            chunk_start_monotonic,
            delivered_monotonic,
        )
        if decoded is not None:
            output.append(decoded)

    def feed(
        self,
        samples: np.ndarray | Iterable[float],
        chunk_start_monotonic: float | None = None,
        delivered_monotonic: float | None = None,
    ) -> list[LTCFrame]:
        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        if values.size == 0:
            return []

        delivered = float(
            delivered_monotonic if delivered_monotonic is not None else time.monotonic()
        )
        chunk_start_sample = self._sample_index
        chunk_start = float(
            chunk_start_monotonic
            if chunk_start_monotonic is not None
            else delivered - values.size / self.sample_rate
        )

        peak = float(np.max(np.abs(values)))
        threshold = max(0.012, min(0.25, peak * 0.18))
        output: list[LTCFrame] = []
        half = self.half_bit_samples

        for sample in values:
            new_state = self._signal_state
            if sample >= threshold:
                new_state = 1
            elif sample <= -threshold:
                new_state = -1

            if self._signal_state == 0:
                self._signal_state = new_state
            elif new_state != 0 and new_state != self._signal_state:
                transition_index = self._sample_index

                if self._last_transition_index is not None:
                    interval = transition_index - self._last_transition_index

                    if 0.45 * half <= interval <= 1.55 * half:
                        self._invalid_transition_count = 0

                        if self._pending_half_interval:
                            self._append_bit(
                                1,
                                output,
                                transition_index,
                                chunk_start_sample,
                                chunk_start,
                                delivered,
                            )
                            self._pending_half_interval = False
                        else:
                            self._pending_half_interval = True

                    elif 1.45 * half < interval <= 2.65 * half:
                        self._invalid_transition_count = 0
                        self._pending_half_interval = False
                        self._append_bit(
                            0,
                            output,
                            transition_index,
                            chunk_start_sample,
                            chunk_start,
                            delivered,
                        )

                    else:
                        self._pending_half_interval = False
                        self._invalid_transition_count += 1

                        if (
                            self._invalid_transition_count
                            >= self._invalid_transition_reset_threshold
                        ):
                            # Do not splice stale bits from before the noise
                            # together with new bits after the signal returns.
                            self._bits.clear()
                            self._last_transition_index = None
                            self._signal_state = 0
                            self._invalid_transition_count = 0

                self._last_transition_index = transition_index
                self._signal_state = new_state

            self._sample_index += 1

        return output


class LTCAudioInput:
    """Low-latency sounddevice input feeding LTCStreamDecoder."""

    def __init__(
        self,
        on_frame: Callable[[LTCFrame], None],
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self._on_frame = on_frame
        self._on_status = on_status or (lambda _message: None)
        self._stream: sd.InputStream | None = None
        self._decoder: LTCStreamDecoder | None = None
        self._lock = threading.RLock()
        self._last_status_at = 0.0
        self._blocksize = 0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._stream is not None and bool(getattr(self._stream, "active", False))

    def start(
        self,
        device_index: int,
        channel_index: int,
        fps: int,
    ) -> tuple[float, int]:
        self.stop()

        fps = int(fps)
        if fps not in SUPPORTED_LTC_FPS:
            raise RuntimeError("LTC input FPS must be 24, 25 or 30.")

        info = sd.query_devices(int(device_index), "input")
        max_channels = int(info.get("max_input_channels", 0))
        if max_channels <= 0:
            raise RuntimeError("Selected device has no input channels.")
        if channel_index < 0 or channel_index >= max_channels:
            raise RuntimeError("Selected LTC input channel is not available.")

        sample_rate = float(info.get("default_samplerate", 48000.0) or 48000.0)
        decoder = LTCStreamDecoder(sample_rate, fps)
        requested_channels = int(channel_index) + 1

        def callback(indata, _frames, time_info, status) -> None:
            callback_now = time.monotonic()
            if status:
                if callback_now - self._last_status_at >= 1.0:
                    self._last_status_at = callback_now
                    self._on_status(f"LTC INPUT STREAM — {status}")

            try:
                channel = np.asarray(indata[:, channel_index], dtype=np.float32)

                # PortAudio timestamps use the same stream clock. Map the ADC
                # capture time to Python's monotonic clock at callback arrival.
                adc_time = float(getattr(time_info, "inputBufferAdcTime", 0.0) or 0.0)
                current_time = float(getattr(time_info, "currentTime", 0.0) or 0.0)
                if adc_time > 0.0 and current_time >= adc_time:
                    chunk_start_monotonic = callback_now - (current_time - adc_time)
                else:
                    chunk_start_monotonic = callback_now - channel.size / sample_rate

                for frame in decoder.feed(
                    channel,
                    chunk_start_monotonic=chunk_start_monotonic,
                    delivered_monotonic=callback_now,
                ):
                    self._on_frame(frame)
            except Exception as exc:
                now = time.monotonic()
                if now - self._last_status_at >= 1.0:
                    self._last_status_at = now
                    self._on_status(f"LTC INPUT DECODE ERROR — {exc}")

        stream = None
        open_errors: list[str] = []
        for blocksize in (256, 128, 0):
            candidate = None
            try:
                candidate = sd.InputStream(
                    device=int(device_index),
                    channels=requested_channels,
                    samplerate=sample_rate,
                    dtype="float32",
                    blocksize=blocksize,
                    latency="low",
                    callback=callback,
                )
                candidate.start()
                stream = candidate
                self._blocksize = blocksize
                break
            except Exception as exc:
                open_errors.append(f"blocksize={blocksize}: {exc}")
                if candidate is not None:
                    try:
                        candidate.close()
                    except Exception:
                        pass

        if stream is None:
            raise RuntimeError("Unable to open LTC input: " + " | ".join(open_errors))

        with self._lock:
            self._decoder = decoder
            self._stream = stream

        block_label = "driver" if self._blocksize == 0 else str(self._blocksize)
        self._on_status(
            f"LTC INPUT STARTED — {fps} FPS — {round(sample_rate)} Hz "
            f"— CH {channel_index + 1} — BUFFER {block_label}"
        )
        return sample_rate, max_channels

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
            self._decoder = None

        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass


def select_latest_offset_group(
    entries: Iterable[tuple[str, float, float]],
    timeline_seconds: float,
    pre_roll_seconds: float = 0.0,
    after_roll_seconds: float = 0.0,
) -> list[str]:
    """Return the most relevant offset group for LTC chase.

    Active tracks have priority. The active range is extended by after-roll.
    When no track is active, the nearest upcoming offset inside pre-roll is
    returned. Every track sharing the selected offset is returned together.
    """

    position = max(
        0.0,
        float(timeline_seconds),
    )
    pre_roll = max(
        0.0,
        float(pre_roll_seconds),
    )
    after_roll = max(
        0.0,
        float(after_roll_seconds),
    )

    active: list[tuple[str, float]] = []
    upcoming: list[tuple[str, float]] = []

    for (
        path,
        offset_seconds,
        duration_seconds,
    ) in entries:
        path_text = str(path)
        offset = max(
            0.0,
            float(offset_seconds),
        )
        duration = max(
            0.0,
            float(duration_seconds),
        )

        end = offset + duration + after_roll

        if offset <= position < end and (duration > 0.0 or position <= offset + after_roll):
            active.append(
                (
                    path_text,
                    offset,
                )
            )
            continue

        if pre_roll > 0.0 and offset - pre_roll <= position < offset:
            upcoming.append(
                (
                    path_text,
                    offset,
                )
            )

    if active:
        chosen_offset = max(offset for _path, offset in active)
        return [path for path, offset in active if abs(offset - chosen_offset) < 0.0005]

    if upcoming:
        chosen_offset = min(offset for _path, offset in upcoming)
        return [path for path, offset in upcoming if abs(offset - chosen_offset) < 0.0005]

    return []
