from __future__ import annotations

import functools
import math
import os
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from PySide6.QtCore import QObject, Signal

from .ltc_encoder import LTCGenerator, ltc_frame_bits
from .streaming import StreamingAudioSource

__all__ = [
    "LTCGenerator",
    "Transport",
    "decode_audio",
    "ltc_frame_bits",
    "make_waveform_preview",
    "requested_output_latency",
]


def requested_output_latency() -> float:
    """Output latency asked of PortAudio, in seconds.

    The default is sized for the Windows default host API, which needs a
    generous block. CoreAudio on macOS and ASIO on Windows are happy far
    lower, and the callback itself uses well under a percent of even a 5 ms
    block. TRACYY_AUDIO_LATENCY tunes it per machine without a rebuild.
    """
    default = 0.05
    raw = str(os.environ.get("TRACYY_AUDIO_LATENCY", "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not 0.002 <= value <= 0.515:
        return default
    return value


def decode_audio(path: str) -> tuple[np.ndarray, int]:
    """Legacy non-realtime full decode helper used only for visual analysis."""
    try:
        data, rate = sf.read(path, always_2d=True, dtype="float32")
        if len(data):
            return data, int(rate)
    except Exception:
        pass

    try:
        import av

        container = av.open(path)
        try:
            stream = next((item for item in container.streams if item.type == "audio"), None)
            if stream is None:
                raise RuntimeError("File không có audio stream")
            rate = int(getattr(stream, "rate", 0) or 48000)
            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=rate)
            blocks: list[np.ndarray] = []
            for frame in container.decode(stream):
                converted = resampler.resample(frame)
                frames = converted if isinstance(converted, list) else [converted]
                for item in frames:
                    if item is None:
                        continue
                    array = item.to_ndarray()
                    if array.ndim == 2 and array.shape[0] <= 8:
                        array = array.T
                    blocks.append(np.ascontiguousarray(array[:, :2], dtype=np.float32))
            if not blocks:
                return np.zeros((0, 2), dtype=np.float32), rate
            return np.concatenate(blocks, axis=0), rate
        finally:
            container.close()
    except Exception as exc:
        raise RuntimeError(
            f"Không đọc được file: {Path(path).name}\n"
            f"Định dạng hoặc codec chưa được hỗ trợ.\n\nChi tiết: {exc}"
        ) from exc


def make_waveform_preview(path: str, points: int = 32768) -> np.ndarray:
    """Create a fast gray amplitude waveform cache with no FFT analysis."""
    try:
        data, _sample_rate = sf.read(
            path,
            always_2d=True,
            dtype="float32",
        )
    except Exception:
        try:
            data, _sample_rate = decode_audio(path)
        except Exception:
            return np.zeros(points, dtype=np.float32)

    data = np.asarray(data, dtype=np.float32)
    if data.size == 0:
        return np.zeros(points, dtype=np.float32)

    mono = data.reshape(-1) if data.ndim == 1 else np.mean(data, axis=1, dtype=np.float32)
    absolute = np.abs(np.asarray(mono, dtype=np.float32).reshape(-1))
    if absolute.size == 0:
        return np.zeros(points, dtype=np.float32)

    points = max(512, int(points))
    if absolute.size <= points:
        source_x = np.linspace(
            0.0,
            1.0,
            absolute.size,
            dtype=np.float32,
        )
        target_x = np.linspace(
            0.0,
            1.0,
            points,
            dtype=np.float32,
        )
        envelope = np.interp(
            target_x,
            source_x,
            absolute,
        ).astype(np.float32, copy=False)
    else:
        # Vectorized peak pooling. Unlike the spectral trial this performs no
        # FFT and no Python loop per waveform point.
        block_size = math.ceil(absolute.size / float(points))
        padded_size = block_size * points
        if padded_size != absolute.size:
            absolute = np.pad(
                absolute,
                (0, padded_size - absolute.size),
                mode="constant",
            )
        envelope = absolute.reshape(
            points,
            block_size,
        ).max(axis=1)

    maximum = float(np.max(envelope)) if envelope.size else 0.0
    if maximum > 1.0e-9:
        envelope = envelope / maximum
    envelope = np.power(
        np.clip(envelope, 0.0, 1.0),
        0.72,
    )
    return np.ascontiguousarray(
        envelope,
        dtype=np.float32,
    )


#: Shared empty cache, so a fresh channel state allocates nothing.
_EMPTY_LTC_CACHE = np.zeros(0, dtype=np.float32)


class _LTCChannelState:
    """Per-output-device LTC continuity.

    V1 stored this as a dict of six string-keyed entries per device and did
    six dictionary lookups per audio callback. Slots make each access an
    attribute load, and give the state a name.
    """

    __slots__ = ("cache", "cache_offset", "frame", "generator", "revision", "scratch")

    def __init__(self, frames: int) -> None:
        self.generator: LTCGenerator | None = None
        self.cache = _EMPTY_LTC_CACHE
        self.cache_offset = 0
        self.frame = -1
        self.revision = -1
        self.scratch = np.zeros(max(8192, int(frames)), dtype=np.float32)

    def scratch_for(self, frames: int) -> np.ndarray:
        """A zeroed view of ``frames`` samples, growing the buffer if needed."""
        if len(self.scratch) < frames:
            self.scratch = np.zeros(max(int(frames), len(self.scratch) * 2), dtype=np.float32)
        output = self.scratch[:frames]
        output.fill(0.0)
        return output


class _VolumeProxy:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def setVolume(self, value: float) -> None:
        with self.transport._lock:
            self.transport.music_gain = max(
                0.0,
                min(1.0, float(value)),
            )


class Transport(QObject):
    status_changed = Signal(str)
    error = Signal(str)
    finished = Signal()
    buffer_ready = Signal(str)
    buffer_state_changed = Signal(str, str, float)

    def __init__(self) -> None:
        super().__init__()

        # Original decoded source audio. It remains untouched so Tracyy
        # can always reopen the output at the source's native sample rate.
        self.audio: StreamingAudioSource | None = None
        self.audio_rate = 48000
        self.duration = 0.0
        self.position_seconds = 0.0
        self.running = False
        self.current_file = ""

        # Playback audio is guaranteed to match the open output stream.
        # When native playback is supported, this references self.audio
        # directly and no resampling/copy is performed.
        self._playback_audio: StreamingAudioSource | None = None
        self._playback_rate = 48000
        self._playback_source_file = ""

        self.music_device: int | None = None
        self.ltc_device: int | None = None
        self.output_device: int | None = None
        self.output_channels = 2
        self.channel_assignments: list[str] = [
            "Music L",
            "Music R",
        ]

        self.fps = 30
        self.ltc_level = 0.65
        self.music_gain = 1.0
        self.timecode_offset_seconds = 0.0

        self.deck_files = ["", ""]
        self.deck_audio: list[StreamingAudioSource | None] = [
            None,
            None,
        ]
        self.deck_rates = [
            48000,
            48000,
        ]
        self.active_deck = 0
        self.deck_offsets = [
            0.0,
            0.0,
        ]

        # Legacy compatibility fields. V1.2.2 no longer stores whole songs
        # as decoded PCM in RAM; only small active/standby streaming windows
        # are retained.
        self.audio_ram_cache: dict[str, tuple[np.ndarray, int]] = {}
        self.audio_ram_cache_bytes = 0
        self._play_when_buffer_ready = False
        self._stream_buffer_seconds = 30.0
        self._stream_startup_seconds = 5.0
        self._standby_buffer_seconds = 12.0

        # Position is counted in prepared playback frames, not source
        # frames. This removes fractional rounding drift between callbacks.
        self._position_frames = 0
        self._position_revision = 0

        # position_seconds only moves when the audio callback delivers a block
        # (~40 ms at the requested latency), so polling it directly makes the
        # playhead step instead of glide. smoothed_position() interpolates
        # between callbacks with the monotonic clock; these fields carry that
        # estimate. The raw value stays authoritative for audio itself.
        self._smooth_position = 0.0
        self._smooth_updated_at = time.perf_counter()
        self._smooth_revision = -1
        self._smooth_running = False
        # The host picks the callback block from the requested latency, and
        # TRACYY_AUDIO_LATENCY can move that from a few ms to half a second.
        # The estimator learns the block from how far position_seconds jumps
        # rather than assuming a size.
        self._smooth_last_raw = 0.0
        self._smooth_block = 0.0

        self._stream: sd.OutputStream | None = None
        self._stream_samplerate = 0
        self._stream_device: int | None = None

        # Let the host select its native callback block size. The requested
        # latency gives Python enough scheduling margin on hosts that need it;
        # see requested_output_latency() for the per-machine override.
        self._requested_latency_seconds = requested_output_latency()

        self._lock = threading.RLock()
        self._volume_proxy = _VolumeProxy(self)

        # Reusable callback buffers. They grow only if the host delivers
        # an unusually large block, then remain allocated.
        self._callback_buffer_size = 8192
        self._mono_scratch = np.zeros(
            self._callback_buffer_size,
            dtype=np.float32,
        )
        self._stereo_scratch = np.zeros(
            (self._callback_buffer_size, 2),
            dtype=np.float32,
        )
        self._ltc_scratch = np.zeros(
            self._callback_buffer_size,
            dtype=np.float32,
        )
        self._callback_status_count = 0
        self._callback_underflow_count = 0
        self._stream_buffer_starvation_count = 0

        self._ltc_generator: LTCGenerator | None = None
        self._ltc_cache = np.zeros(
            0,
            dtype=np.float32,
        )
        self._ltc_cache_offset = 0
        self._ltc_frame = -1
        self._ltc_revision = -1

        # Multi-device output configuration. The first configuration with a
        # Music assignment becomes the playback clock/master stream. Extra
        # soundcards follow that master position and never advance it.
        self.output_configurations: list[dict[str, object]] = []
        self._extra_output_configurations: list[dict[str, object]] = []
        self._extra_streams: dict[
            int,
            sd.OutputStream,
        ] = {}
        # Each physical output device has its own hardware clock and callback
        # cadence. Never make follower callbacks read the master's mutable
        # frame cursor directly; doing that repeats/skips blocks whenever the
        # callbacks arrive at different times and is heard as crackle/popping.
        # A follower advances its own immutable-buffer cursor and is reset only
        # on an explicit transport revision (seek/stop/pause/load/reconfigure).
        self._extra_stream_states: dict[
            int,
            dict[str, object],
        ] = {}
        self._device_underflow_counts: dict[int, int] = {}
        self._ltc_states: dict[
            str,
            dict[str, object],
        ] = {}
        self._output_config_revision = 0
        self._opened_output_config_revision = -1

    def _on_stream_source_state(
        self,
        path: str,
        state: str,
        ahead_seconds: float,
    ) -> None:
        self.buffer_state_changed.emit(
            str(path),
            str(state),
            float(ahead_seconds),
        )

    def _on_stream_source_ready(self, path: str) -> None:
        normalized = str(Path(path).resolve())
        should_start = False
        rate = 0
        with self._lock:
            source = self._playback_audio
            if (
                source is not None
                and self.current_file == normalized
                and source.path == normalized
            ):
                self.buffer_ready.emit(normalized)
                if self._play_when_buffer_ready and not self.running:
                    self._play_when_buffer_ready = False
                    self.running = True
                    source.notify_position(self._position_frames)
                    should_start = True
                    rate = int(self._playback_rate)
            else:
                self.buffer_ready.emit(normalized)

        if should_start:
            self.status_changed.emit(f"PLAYING — STREAM {rate} HZ")

    def is_buffer_ready(self, path: str | None = None) -> bool:
        normalized = str(Path(path).resolve()) if path else ""
        with self._lock:
            candidates = [item for item in self.deck_audio if item is not None]
            current_position = int(self._position_frames)
            current_file = self.current_file
        for source in candidates:
            if normalized and source.path != normalized:
                continue
            frame = current_position if source.path == current_file else 0
            if source.ready(frame):
                return True
        return False

    def buffer_seconds_ahead(self) -> float:
        with self._lock:
            source = self._playback_audio
            frame = int(self._position_frames)
        if source is None:
            return 0.0
        return float(source.buffered_seconds_ahead(frame))

    @property
    def audio_output(self) -> _VolumeProxy:
        return self._volume_proxy

    @property
    def standby_deck(self) -> int:
        return 1 - self.active_deck

    @property
    def standby_file(self) -> str:
        return self.deck_files[self.standby_deck]

    def cached_audio(
        self,
        path: str,
    ) -> None:
        # Whole-song PCM caching was removed in V1.2.2. Kept only so older
        # controller code/plugins do not crash while migrating.
        return None

    def cache_audio(
        self,
        path: str,
        audio: np.ndarray,
        rate: int,
    ) -> None:
        # Intentionally no-op: Tracyy V1.2.2 streams decoded PCM through a
        # bounded look-ahead buffer instead of retaining full songs in RAM.
        del path, audio, rate
        self.audio_ram_cache_bytes = 0

    def preload_audio_to_ram(
        self,
        path: str,
    ) -> tuple[None, int]:
        # Compatibility shim. Prepare a bounded Standby stream instead of
        # decoding the whole track into memory.
        self.preload_music(path)
        source = self.deck_audio[self.standby_deck]
        rate = int(source.output_rate if source is not None else 48000)
        return None, rate

    def clear_audio_ram_cache(
        self,
        *,
        keep_paths: list[str] | None = None,
    ) -> None:
        del keep_paths
        self.audio_ram_cache.clear()
        self.audio_ram_cache_bytes = 0

    def _decode_audio_uncached(
        self,
        path: str,
    ) -> tuple[np.ndarray, int]:
        # Full decode remains available only for legacy callers. It is never
        # used by the realtime transport/playlist preload path in V1.2.2.
        try:
            data, rate = sf.read(
                path,
                dtype="float32",
                always_2d=True,
            )
        except Exception as exc:
            raise RuntimeError(
                "Legacy full decode only supports formats available through "
                f"libsndfile: {Path(path).name}: {exc}"
            ) from exc

        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        elif data.shape[1] > 2:
            data = data[:, :2]
        return np.ascontiguousarray(data, dtype=np.float32), int(rate)

    def _decode_audio(
        self,
        path: str,
    ) -> tuple[np.ndarray, int]:
        return self._decode_audio_uncached(str(Path(path).resolve()))

    def load_audio(self, path: str) -> None:
        path = str(Path(path).resolve())
        try:
            source = StreamingAudioSource(
                path,
                buffer_seconds=self._stream_buffer_seconds,
                startup_seconds=self._stream_startup_seconds,
                on_ready=self._on_stream_source_ready,
                on_state=self._on_stream_source_state,
            )
            # Start immediately at the native file rate. In the common case
            # the output also uses this rate, so the first 5 seconds are being
            # prepared before the user presses Play.
            source.start(source.source_rate, 0.0)
        except Exception as exc:
            self.error.emit(f"Không đọc được audio: {exc}")
            return

        self.stop()

        with self._lock:
            old_source = self.deck_audio[self.active_deck]
            self.deck_audio[self.active_deck] = source
            self.deck_rates[self.active_deck] = int(source.source_rate)
            self.deck_files[self.active_deck] = path

            self.audio = source
            self.audio_rate = int(source.source_rate)
            self.duration = float(source.duration)
            self.position_seconds = 0.0
            self._position_frames = 0
            self.current_file = path
            self.running = False
            self._play_when_buffer_ready = False
            self._position_revision += 1

            self._playback_audio = source
            self._playback_rate = int(source.output_rate)
            self._playback_source_file = path

        if old_source is not None and old_source is not source:
            try:
                old_source.close()
            except Exception:
                pass

        self.status_changed.emit(f"BUFFERING — {Path(path).name}")

    def preload_music(self, path: str) -> None:
        deck = self.standby_deck

        if path:
            path = str(Path(path).resolve())

        if not path:
            with self._lock:
                old = self.deck_audio[deck]
                self.deck_audio[deck] = None
                self.deck_files[deck] = ""
                self.deck_rates[deck] = 48000
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            return

        if path == self.current_file:
            return

        with self._lock:
            existing = self.deck_audio[deck]
            existing_path = self.deck_files[deck]
        if existing is not None and existing_path == path:
            return

        try:
            source = StreamingAudioSource(
                path,
                buffer_seconds=self._standby_buffer_seconds,
                startup_seconds=min(5.0, self._standby_buffer_seconds * 0.5),
                on_ready=self._on_stream_source_ready,
                on_state=self._on_stream_source_state,
            )
            # Prefer the currently-open common output rate so promotion can be
            # instantaneous. Otherwise use the source's native rate.
            target_rate = int(self._stream_samplerate or source.source_rate)
            source.start(target_rate, 0.0)
        except Exception:
            return

        with self._lock:
            old = self.deck_audio[deck]
            self.deck_audio[deck] = source
            self.deck_rates[deck] = int(source.output_rate)
            self.deck_files[deck] = path
            self.deck_offsets[deck] = 0.0
        if old is not None and old is not source:
            try:
                old.close()
            except Exception:
                pass

    def promote_preloaded_music(
        self,
    ) -> str | None:
        new_deck = self.standby_deck

        with self._lock:
            new_file = self.deck_files[new_deck]
            new_source = self.deck_audio[new_deck]
            new_rate = self.deck_rates[new_deck]

        if not new_file or new_source is None:
            return None

        self.pause()
        old_deck = self.active_deck
        old_source = self.deck_audio[old_deck]
        self.active_deck = new_deck

        with self._lock:
            self.current_file = new_file
            self.audio = new_source
            self.audio_rate = int(new_source.source_rate)
            self.duration = float(new_source.duration)
            self.position_seconds = 0.0
            self._position_frames = 0
            self.running = False
            self._play_when_buffer_ready = False
            self._position_revision += 1

            self._playback_audio = new_source
            self._playback_rate = int(new_source.output_rate or new_rate)
            self._playback_source_file = new_file

            self.deck_audio[old_deck] = None
            self.deck_files[old_deck] = ""
            self.deck_rates[old_deck] = 48000

        new_source.notify_position(0)
        if old_source is not None and old_source is not new_source:
            try:
                old_source.close()
            except Exception:
                pass
        return new_file

    def configure(
        self,
        output_device: int | None,
        channel_assignments: list[str],
        fps: int,
        ltc_level: int,
        music_gain: int,
    ) -> None:
        self.configure_multiple(
            [
                {
                    "device_index": output_device,
                    "assignments": list(channel_assignments),
                }
            ],
            fps,
            ltc_level,
            music_gain,
        )

    def configure_multiple(
        self,
        output_configurations: list[dict[str, object]],
        fps: int,
        ltc_level: int,
        music_gain: int,
    ) -> None:
        gain = max(
            0.0,
            min(
                1.0,
                music_gain / 100.0,
            ),
        )

        normalized: list[dict[str, object]] = []
        used_devices: set[int] = set()

        for raw in output_configurations:
            raw_index = raw.get("device_index")

            if raw_index is None:
                continue

            device_index = int(raw_index)

            if device_index in used_devices:
                raise RuntimeError("Each output soundcard may only be configured once.")

            used_devices.add(device_index)

            info = sd.query_devices(
                device_index,
                "output",
            )
            max_channels = int(
                info.get(
                    "max_output_channels",
                    0,
                )
            )
            if max_channels <= 0:
                raise RuntimeError(f"Output device {device_index} has no output channels.")

            assignments = [str(value) for value in raw.get("assignments", [])][:max_channels]

            if len(assignments) < max_channels:
                assignments.extend(["Off"] * (max_channels - len(assignments)))

            normalized.append(
                {
                    "device_index": device_index,
                    "device_name": str(
                        raw.get(
                            "device_name",
                            info.get(
                                "name",
                                "",
                            ),
                        )
                    ),
                    "channels": max_channels,
                    "assignments": assignments,
                }
            )

        if not normalized:
            raise RuntimeError("No valid output soundcard is configured.")

        master_index = 0
        for index, config in enumerate(normalized):
            if any(str(assignment).startswith("Music") for assignment in config["assignments"]):
                master_index = index
                break

        if master_index != 0:
            master = normalized.pop(master_index)
            normalized.insert(
                0,
                master,
            )

        master = normalized[0]

        with self._lock:
            self.output_configurations = [
                {
                    "device_index": int(config["device_index"]),
                    "device_name": str(
                        config.get(
                            "device_name",
                            "",
                        )
                    ),
                    "channels": int(config["channels"]),
                    "assignments": list(config["assignments"]),
                }
                for config in normalized
            ]

            self._extra_output_configurations = [
                dict(config) for config in self.output_configurations[1:]
            ]

            self.output_device = int(master["device_index"])
            self.music_device = int(master["device_index"])

            ltc_config = next(
                (
                    config
                    for config in self.output_configurations
                    if "LTC" in config["assignments"]
                ),
                master,
            )
            self.ltc_device = int(ltc_config["device_index"])

            self.output_channels = int(master["channels"])
            self.channel_assignments = list(master["assignments"])
            self.fps = int(fps)
            self.ltc_level = max(
                0.05,
                min(
                    1.0,
                    ltc_level / 100.0,
                ),
            )
            self.music_gain = gain
            self._position_revision += 1
            self._output_config_revision += 1

    def _output_supports_rate(
        self,
        samplerate: int,
        channels: int,
    ) -> bool:
        try:
            sd.check_output_settings(
                device=self.output_device,
                samplerate=float(samplerate),
                channels=int(channels),
                dtype="float32",
            )
            return True
        except Exception:
            return False

    def _effective_output_latency(
        self,
        info: dict,
    ) -> float:
        """Requested latency, raised to what the device can actually sustain.

        PortAudio does not refuse a latency below the device minimum. On
        CoreAudio it silently falls back to a ~15 frame callback block — a
        0.3 ms deadline, 3200 callbacks a second — which no Python callback
        survives next to the Qt thread, because the GIL switch interval alone
        is 5 ms. Bluetooth output is the case that hits this: AirPods report
        a 171 ms minimum, sixteen times what TRACYY_AUDIO_LATENCY asks for by
        default. Wired interfaces report a few ms and keep the requested value.
        """
        requested = float(self._requested_latency_seconds)
        try:
            device_floor = float(info.get("default_high_output_latency", 0.0) or 0.0)
        except (TypeError, ValueError):
            device_floor = 0.0
        return max(requested, device_floor * 1.2)

    def _choose_output_samplerate(
        self,
        channels: int,
        default_samplerate: float,
    ) -> int:
        with self._lock:
            source_rate = int(self.audio_rate)
            has_audio = self.audio is not None and len(self.audio) > 0

        candidates: list[int] = []
        if has_audio and source_rate > 0:
            candidates.append(source_rate)

        candidates.extend(
            [
                round(default_samplerate),
                48000,
                44100,
            ]
        )

        seen: set[int] = set()
        for candidate in candidates:
            if candidate <= 0 or candidate in seen:
                continue
            seen.add(candidate)

            if self._output_supports_rate(
                candidate,
                channels,
            ):
                return candidate

        raise RuntimeError("Thiết bị không hỗ trợ sample rate output phù hợp.")

    def _resample_stereo_linear(
        self,
        audio: np.ndarray,
        source_rate: int,
        target_rate: int,
    ) -> np.ndarray:
        if source_rate == target_rate or len(audio) == 0:
            return audio

        if len(audio) == 1:
            target_length = max(
                1,
                round(target_rate / float(source_rate)),
            )
            return np.repeat(
                audio,
                target_length,
                axis=0,
            ).astype(
                np.float32,
                copy=False,
            )

        target_length = max(
            1,
            round(len(audio) * target_rate / float(source_rate)),
        )
        output = np.empty(
            (
                target_length,
                2,
            ),
            dtype=np.float32,
        )

        source_per_target = source_rate / float(target_rate)
        chunk_size = 262144

        for start in range(
            0,
            target_length,
            chunk_size,
        ):
            end = min(
                target_length,
                start + chunk_size,
            )
            positions = np.arange(
                start,
                end,
                dtype=np.float64,
            )
            positions *= source_per_target
            np.minimum(
                positions,
                len(audio) - 1,
                out=positions,
            )

            left_indexes = np.floor(positions).astype(
                np.int64,
                copy=False,
            )
            right_indexes = np.minimum(
                left_indexes + 1,
                len(audio) - 1,
            )
            fractions = (positions - left_indexes).astype(
                np.float32,
                copy=False,
            )
            inverse = 1.0 - fractions

            output[start:end, 0] = (
                audio[
                    left_indexes,
                    0,
                ]
                * inverse
                + audio[
                    right_indexes,
                    0,
                ]
                * fractions
            )
            output[start:end, 1] = (
                audio[
                    left_indexes,
                    1,
                ]
                * inverse
                + audio[
                    right_indexes,
                    1,
                ]
                * fractions
            )

        return np.ascontiguousarray(
            output,
            dtype=np.float32,
        )

    def _prepare_playback_audio(
        self,
        output_rate: int,
    ) -> None:
        with self._lock:
            source = self.audio
            source_file = self.current_file
            position = float(self.position_seconds)

        if source is None:
            with self._lock:
                self._playback_audio = None
                self._playback_rate = int(output_rate)
                self._playback_source_file = source_file
                self._position_frames = 0
            return

        # Restart only when sample rate/seek target requires it. Decode and
        # resample happen in the child process; no full-track PCM conversion is
        # performed on the GUI thread.
        desired_frame = round(position * int(output_rate))
        pipeline_matches = int(source.output_rate) == int(output_rate) and abs(
            int(getattr(source, "_position_frame", desired_frame)) - desired_frame
        ) < max(1, int(output_rate * 0.050))
        if not pipeline_matches:
            source.start(int(output_rate), position)

        with self._lock:
            if source_file != self.current_file or source is not self.audio:
                return
            self._playback_audio = source
            self._playback_rate = int(output_rate)
            self._playback_source_file = source_file
            self._position_frames = min(
                len(source),
                round(position * int(output_rate)),
            )
            source.notify_position(self._position_frames)
            self._position_revision += 1

    def _playback_pipeline_matches(
        self,
        output_rate: int,
    ) -> bool:
        with self._lock:
            return (
                self._playback_audio is not None
                and self._playback_source_file == self.current_file
                and int(self._playback_rate) == int(output_rate)
            )

    def current_position(self) -> float:
        with self._lock:
            return max(
                0.0,
                min(
                    self.duration,
                    self.position_seconds,
                ),
            )

    def audio_health(self) -> dict[str, object]:
        """Callback statistics, for judging whether the latency is safe.

        The callback already counts dropouts per device; nothing read them.
        Underflows staying at zero through a full show is what says a given
        TRACYY_AUDIO_LATENCY value is actually safe on this machine.
        """
        with self._lock:
            return {
                "requested_latency": float(self._requested_latency_seconds),
                "samplerate": int(self._stream_samplerate),
                "status_events": int(self._callback_status_count),
                "underflows": int(self._callback_underflow_count),
                "underflows_by_device": dict(self._device_underflow_counts),
                "buffer_starvations": int(self._stream_buffer_starvation_count),
            }

    def smoothed_position(self) -> float:
        """Playhead time interpolated between two audio callbacks.

        The stream callback advances position_seconds once per delivered
        block, so a UI polling it every 15 ms sees the same value two or
        three times and then a jump of a whole block. This returns the same
        timeline, continuous: the last reported position carried forward by
        the monotonic clock, gently pulled back onto the real one instead of
        snapping. It never runs backwards during playback, and it falls back
        to the exact position whenever the transport is not playing.
        """
        now = time.perf_counter()
        with self._lock:
            duration = float(self.duration)
            raw = max(
                0.0,
                min(duration, float(self.position_seconds)),
            )
            running = bool(self.running)
            revision = int(self._position_revision)

            # Seek, load and stop bump the revision; those are hard cuts and
            # must be shown exactly, not eased into.
            reanchor = (
                not running or not self._smooth_running or revision != self._smooth_revision
            )

            jump = raw - self._smooth_last_raw
            if jump > 0.0:
                # Decaying maximum: reacts at once to a larger block and
                # settles back down if the stream is reopened smaller.
                self._smooth_block = max(jump, self._smooth_block * 0.95)
            self._smooth_last_raw = raw
            block = self._smooth_block

            if reanchor:
                estimate = raw
                self._smooth_block = 0.0
            else:
                elapsed = max(0.0, now - self._smooth_updated_at)
                estimate = self._smooth_position + elapsed
                drift = raw - estimate

                if drift > max(0.25, block * 1.5):
                    # Further behind than a whole block can explain, e.g. a
                    # stall that resolved itself. Cut to the truth.
                    estimate = raw
                else:
                    # The raw value is the end of the block that is only now
                    # being played, so the estimate normally trails it by
                    # about half a block. Absorb the difference over a time
                    # constant scaled to the block, otherwise a large block
                    # drags the playhead along its staircase.
                    tau = max(0.15, block * 1.5)
                    estimate += drift * (1.0 - math.exp(-elapsed / tau))
                    estimate = max(estimate, self._smooth_position)

                # A starved decoder leaves position_seconds standing still
                # while running stays true. The estimate must stall with it
                # rather than run ahead and later snap backwards.
                estimate = min(estimate, raw + max(0.050, block * 0.5))
                estimate = max(0.0, min(duration, estimate))
                estimate = max(estimate, self._smooth_position)

            self._smooth_position = estimate
            self._smooth_updated_at = now
            self._smooth_revision = revision
            self._smooth_running = running
            return estimate

    def seek(self, seconds: float) -> None:
        with self._lock:
            target = max(0.0, min(self.duration, float(seconds)))
            playback_rate = max(1, int(self._playback_rate or self.audio_rate))
            source = self._playback_audio
            was_running = bool(self.running)
            target_frame = min(
                len(source) if source is not None else round(self.duration * playback_rate),
                round(target * playback_rate),
            )
            self.position_seconds = target
            self._position_frames = target_frame
            self._position_revision += 1
            # A seek invalidates the old buffered window. If playback was
            # active, resume automatically only after the new startup window
            # is available.
            if was_running:
                self.running = False
                self._play_when_buffer_ready = True

        if source is not None:
            source.start(playback_rate, target)
            source.notify_position(target_frame)
            if source.ready(target_frame) and was_running:
                with self._lock:
                    self._play_when_buffer_ready = False
                    self.running = True
            elif was_running:
                self.status_changed.emit(f"BUFFERING SEEK — {Path(self.current_file).name}")

    def _reset_ltc_state(self) -> None:
        """Drop per-device LTC continuity. Called on stop/pause/reconfigure."""
        with self._lock:
            self._ltc_states = {}

    def _next_ltc_samples(
        self,
        frames: int,
        samplerate: float,
        position: float,
        running: bool,
        state_key: object = "master",
    ) -> np.ndarray:
        """One block of LTC audio for one output device.

        Realtime path. V1 took ``self._lock`` here on every callback, on every
        device, purely to read four scalars — while the GUI thread could be
        holding the same lock inside ``load_audio`` or ``configure_multiple``.
        The scalars are read directly now: each is a single attribute load,
        atomic under CPython, and the worst case for reading a stale one is
        that a single 33 ms LTC frame uses the previous level. The state dict
        is only mutated under the lock when a device is first seen.
        """
        fps = int(self.fps)
        level = float(self.ltc_level)
        revision = int(self._position_revision)
        offset = float(self.timecode_offset_seconds)

        key = str(state_key)
        state = self._ltc_states.get(key)
        if state is None:
            state = _LTCChannelState(frames)
            with self._lock:
                self._ltc_states[key] = state

        output = state.scratch_for(frames)
        desired_frame = max(0, int((position + offset) * fps))

        generator = state.generator
        if (
            generator is None
            or generator.fps != fps
            or revision != state.revision
            or abs(generator.sample_rate - samplerate) > 0.5
        ):
            generator = LTCGenerator(samplerate, fps, level)
            state.generator = generator
            state.cache = _EMPTY_LTC_CACHE
            state.cache_offset = 0
            state.frame = desired_frame - 1
            state.revision = revision

        generator.level = level
        write = 0

        while write < frames:
            if state.cache_offset >= len(state.cache):
                current_frame = state.frame
                if running:
                    # Advance one frame at a time so the biphase carrier stays
                    # continuous; only resync when the timeline actually jumped.
                    if abs((current_frame + 1) - desired_frame) > 1:
                        current_frame = desired_frame
                    else:
                        current_frame += 1
                else:
                    current_frame = desired_frame

                state.frame = current_frame
                state.cache = generator.encode(current_frame)
                state.cache_offset = 0

            take = min(len(state.cache) - state.cache_offset, frames - write)
            output[write : write + take] = state.cache[
                state.cache_offset : state.cache_offset + take
            ]
            write += take
            state.cache_offset += take

        return output

    def _ensure_callback_buffers(
        self,
        frames: int,
    ) -> None:
        if frames <= self._callback_buffer_size:
            return

        new_size = max(
            int(frames),
            self._callback_buffer_size * 2,
        )
        self._mono_scratch = np.zeros(
            new_size,
            dtype=np.float32,
        )
        self._stereo_scratch = np.zeros(
            (new_size, 2),
            dtype=np.float32,
        )
        self._ltc_scratch = np.zeros(
            new_size,
            dtype=np.float32,
        )
        self._callback_buffer_size = new_size

    def _fill_music_channels_from_position(
        self,
        outdata: np.ndarray,
        frames: int,
        assignments: list[str],
        output_rate: int,
        source_position: int,
        audio: StreamingAudioSource | None,
        playback_rate: int,
        gain: float,
        mono_scratch: np.ndarray,
        stereo_scratch: np.ndarray,
    ) -> tuple[int, bool]:
        """Render one buffered block without disk/codec work in callback."""
        if audio is None or len(audio) == 0 or int(playback_rate) != int(output_rate):
            return int(source_position), False

        source_position = max(0, min(len(audio), int(source_position)))
        request = min(int(frames), max(0, len(audio) - source_position))
        if request <= 0:
            return source_position, source_position >= len(audio)

        stereo = stereo_scratch[:request, :2]
        count, ended = audio.read_into(stereo, source_position)
        count = max(0, min(request, int(count)))
        end = source_position + count

        if count < request and not ended:
            # Decoder is temporarily behind. Do not advance over missing PCM;
            # the next callback resumes at exactly the first unavailable frame.
            self._stream_buffer_starvation_count += 1

        if count > 0:
            source = stereo[:count]
            limit = min(outdata.shape[1], len(assignments))
            mono_ready = False
            mono = mono_scratch[:count]

            for channel in range(limit):
                assignment = assignments[channel]

                if assignment == "Music L":
                    if abs(gain - 1.0) < 1e-9:
                        outdata[:count, channel] = source[:, 0]
                    else:
                        np.multiply(
                            source[:, 0],
                            gain,
                            out=outdata[:count, channel],
                        )

                elif assignment == "Music R":
                    if abs(gain - 1.0) < 1e-9:
                        outdata[:count, channel] = source[:, 1]
                    else:
                        np.multiply(
                            source[:, 1],
                            gain,
                            out=outdata[:count, channel],
                        )

                elif assignment == "Music Mono":
                    if not mono_ready:
                        np.add(source[:, 0], source[:, 1], out=mono)
                        np.multiply(mono, 0.5 * gain, out=mono)
                        mono_ready = True
                    outdata[:count, channel] = mono

        return end, bool(ended or end >= len(audio))

    def _render_music_channels(
        self,
        outdata: np.ndarray,
        frames: int,
        assignments: list[str],
        output_rate: int,
        advance_position: bool = True,
    ) -> bool:
        with self._lock:
            audio = self._playback_audio
            playback_rate = int(self._playback_rate)
            source_position = int(self._position_frames)
            running = bool(self.running)
            gain = float(self.music_gain)
            revision = int(self._position_revision)

        if not running:
            return False

        end, ended = self._fill_music_channels_from_position(
            outdata,
            frames,
            assignments,
            output_rate,
            source_position,
            audio,
            playback_rate,
            gain,
            self._mono_scratch,
            self._stereo_scratch,
        )

        if advance_position:
            with self._lock:
                if (
                    revision == self._position_revision
                    and self.running
                    and audio is self._playback_audio
                ):
                    self._position_frames = end
                    self.position_seconds = min(
                        self.duration,
                        end / float(max(1, playback_rate)),
                    )
                    if audio is not None:
                        audio.notify_position(end)

                    if ended:
                        self.running = False
                        self.position_seconds = self.duration
                        self._position_revision += 1

        return bool(ended and advance_position)

    def _stream_callback(
        self,
        outdata,
        frames,
        time_info,
        status,
    ) -> None:
        del time_info

        self._ensure_callback_buffers(frames)
        outdata.fill(0.0)

        if status:
            self._callback_status_count += 1
            if getattr(
                status,
                "output_underflow",
                False,
            ):
                self._callback_underflow_count += 1

        with self._lock:
            running = bool(self.running)
            position = float(self.position_seconds)
            assignments = self.channel_assignments
            output_rate = int(self._stream_samplerate or self._playback_rate or 48000)
            device_key = self.output_device if self.output_device is not None else "master"

        ended = self._render_music_channels(
            outdata,
            frames,
            assignments,
            output_rate,
            advance_position=True,
        )

        if running and "LTC" in assignments:
            ltc = self._next_ltc_samples(
                frames,
                output_rate,
                position,
                True,
                state_key=device_key,
            )
            limit = min(
                outdata.shape[1],
                len(assignments),
            )
            for channel in range(limit):
                if assignments[channel] == "LTC":
                    outdata[
                        :,
                        channel,
                    ] = ltc

        if ended:
            # Emitted directly, not through QTimer.singleShot. PortAudio's
            # callback thread is not a QThread, so a timer created here has no
            # event loop to fire it and the signal was silently dropped — the
            # track ended, but nothing in the UI ever heard about it. A plain
            # emit is queued by Qt to the transport's own thread instead, so no
            # slot runs on the audio thread either.
            self.finished.emit()

    def _close_extra_output_streams(
        self,
    ) -> None:
        streams = list(self._extra_streams.values())
        self._extra_streams = {}

        for stream in streams:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

        self._extra_stream_states = {}

    def _extra_stream_callback(
        self,
        device_index: int,
        assignments: list[str],
        outdata,
        frames,
        time_info,
        status,
    ) -> None:
        del time_info
        outdata.fill(0.0)

        if status:
            self._callback_status_count += 1
            if getattr(status, "output_underflow", False):
                self._callback_underflow_count += 1
                self._device_underflow_counts[device_index] = (
                    self._device_underflow_counts.get(device_index, 0) + 1
                )

        state = self._extra_stream_states.get(device_index)
        if state is None:
            return

        with self._lock:
            running = bool(self.running)
            master_position_frames = int(self._position_frames)
            revision = int(self._position_revision)
            audio = self._playback_audio
            playback_rate = int(self._playback_rate)
            gain = float(self.music_gain)
            position_seconds = float(self.position_seconds)
            output_rate = int(self._stream_samplerate or self._playback_rate or 48000)

        # Seek/stop/pause/load/reconfigure explicitly changes the transport
        # revision. Only then do we realign the follower cursor. During normal
        # playback the follower advances locally so asynchronous callbacks can
        # never repeat or skip arbitrary master blocks.
        if int(state.get("revision", -1)) != revision:
            state["position_frames"] = master_position_frames
            state["revision"] = revision

        if not running:
            return

        mono_scratch = state.get("mono_scratch")
        if not isinstance(mono_scratch, np.ndarray) or len(mono_scratch) < frames:
            mono_scratch = np.zeros(
                max(8192, int(frames) * 2),
                dtype=np.float32,
            )
            state["mono_scratch"] = mono_scratch
        stereo_scratch = state.get("stereo_scratch")
        if (
            not isinstance(stereo_scratch, np.ndarray)
            or stereo_scratch.ndim != 2
            or len(stereo_scratch) < frames
        ):
            stereo_scratch = np.zeros(
                (max(8192, int(frames) * 2), 2),
                dtype=np.float32,
            )
            state["stereo_scratch"] = stereo_scratch

        local_position = int(state.get("position_frames", master_position_frames))

        # Independent hardware clocks will drift slowly. Do not chase normal
        # block-to-block timing differences (that caused the old crackle). A
        # very large discontinuity means the follower missed a substantial
        # amount of audio, so recover once at a coarse threshold.
        hard_realign_threshold = max(
            int(output_rate * 0.250),
            int(frames) * 8,
        )
        if abs(master_position_frames - local_position) > hard_realign_threshold:
            local_position = master_position_frames

        end, _ended = self._fill_music_channels_from_position(
            outdata,
            frames,
            assignments,
            output_rate,
            local_position,
            audio,
            playback_rate,
            gain,
            mono_scratch,
            state["stereo_scratch"],
        )
        state["position_frames"] = end

        if running and "LTC" in assignments:
            ltc = self._next_ltc_samples(
                frames,
                output_rate,
                position_seconds,
                True,
                state_key=device_index,
            )
            limit = min(outdata.shape[1], len(assignments))
            for channel in range(limit):
                if assignments[channel] == "LTC":
                    outdata[:, channel] = ltc

    def _open_extra_output_streams(
        self,
        samplerate: int,
    ) -> None:
        self._close_extra_output_streams()

        opened: dict[
            int,
            sd.OutputStream,
        ] = {}

        try:
            for config in self._extra_output_configurations:
                device_index = int(config["device_index"])
                channels = int(config["channels"])
                assignments = list(config["assignments"])

                device_info = sd.query_devices(device_index, "output")

                sd.check_output_settings(
                    device=device_index,
                    samplerate=float(samplerate),
                    channels=channels,
                    dtype="float32",
                )

                # partial binds the device and its assignments once, instead
                # of building a closure PortAudio has to call through on every
                # block.
                callback = functools.partial(
                    self._extra_stream_callback,
                    device_index,
                    assignments,
                )

                stream = sd.OutputStream(
                    device=device_index,
                    samplerate=float(samplerate),
                    channels=channels,
                    dtype="float32",
                    blocksize=0,
                    latency=self._effective_output_latency(device_info),
                    callback=callback,
                )
                with self._lock:
                    self._extra_stream_states[device_index] = {
                        "position_frames": int(self._position_frames),
                        "revision": int(self._position_revision),
                        "mono_scratch": np.zeros(
                            self._callback_buffer_size,
                            dtype=np.float32,
                        ),
                        "stereo_scratch": np.zeros(
                            (self._callback_buffer_size, 2),
                            dtype=np.float32,
                        ),
                    }
                stream.start()
                opened[device_index] = stream

        except Exception:
            for stream in opened.values():
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            self._extra_stream_states = {}
            raise

        self._extra_streams = opened

    def _ensure_default_output_configuration(
        self,
    ) -> None:
        if self.output_configurations:
            master = self.output_configurations[0]
            self.output_device = int(master["device_index"])
            self.output_channels = int(master["channels"])
            self.channel_assignments = list(master["assignments"])
            self._extra_output_configurations = [
                dict(config) for config in self.output_configurations[1:]
            ]
            return

        default_output = sd.default.device[1]
        device_index = (
            int(default_output)
            if default_output is not None and int(default_output) >= 0
            else None
        )

        if device_index is None:
            for index, device in enumerate(sd.query_devices()):
                if (
                    int(
                        device.get(
                            "max_output_channels",
                            0,
                        )
                    )
                    > 0
                ):
                    device_index = index
                    break

        if device_index is None:
            raise RuntimeError("Không tìm thấy thiết bị âm thanh output.")

        info = sd.query_devices(
            device_index,
            "output",
        )
        channel_count = int(
            info.get(
                "max_output_channels",
                0,
            )
        )

        if channel_count <= 0:
            raise RuntimeError("Thiết bị mặc định không có output channel.")

        if channel_count == 1:
            assignments = ["Music Mono"]
        else:
            assignments = [
                "Music L",
                "Music R",
            ]
            assignments.extend(["Off"] * (channel_count - 2))

        self.configure_multiple(
            [
                {
                    "device_index": int(device_index),
                    "device_name": str(
                        info.get(
                            "name",
                            "",
                        )
                    ),
                    "assignments": assignments,
                }
            ],
            self.fps,
            round(self.ltc_level * 100.0),
            round(self.music_gain * 100.0),
        )

    def _open_stream(
        self,
        samplerate: int | None = None,
    ) -> None:
        self._ensure_default_output_configuration()
        self._close_extra_output_streams()

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._stream_samplerate = 0
            self._stream_device = None

        info = sd.query_devices(
            self.output_device,
            "output",
        )
        default_samplerate = float(
            info.get(
                "default_samplerate",
                48000.0,
            )
        )
        channels = int(
            info.get(
                "max_output_channels",
                0,
            )
        )

        if channels <= 0:
            raise RuntimeError("Thiết bị không có output channel.")

        if samplerate is None:
            samplerate = self._choose_output_samplerate(
                channels,
                default_samplerate,
            )
        else:
            samplerate = int(samplerate)

        if not self._output_supports_rate(
            samplerate,
            channels,
        ):
            raise RuntimeError(f"Thiết bị không hỗ trợ {samplerate} Hz.")

        # All soundcards use one common rate so follower streams can render the
        # same prepared audio buffer and master timeline position.
        for config in self._extra_output_configurations:
            try:
                sd.check_output_settings(
                    device=int(config["device_index"]),
                    samplerate=float(samplerate),
                    channels=int(config["channels"]),
                    dtype="float32",
                )
            except Exception as exc:
                name = str(
                    config.get(
                        "device_name",
                        config["device_index"],
                    )
                )
                raise RuntimeError(
                    f"{name} does not support the common {samplerate} Hz output rate: {exc}"
                ) from exc

        self._prepare_playback_audio(int(samplerate))
        self._reset_ltc_state()

        stream = sd.OutputStream(
            device=self.output_device,
            samplerate=float(samplerate),
            channels=channels,
            dtype="float32",
            blocksize=0,
            latency=self._effective_output_latency(info),
            callback=self._stream_callback,
        )

        self._stream = stream
        self._stream_samplerate = int(samplerate)
        self._stream_device = int(self.output_device)

        try:
            stream.start()
            self._open_extra_output_streams(int(samplerate))
            self._opened_output_config_revision = self._output_config_revision

        except Exception:
            self._close_extra_output_streams()
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

            self._stream = None
            self._stream_samplerate = 0
            self._stream_device = None
            raise

    def play(self) -> None:
        if not self.current_file or self.audio is None:
            self.error.emit("Chưa chọn bài nhạc.")
            return

        if self.current_position() >= self.duration - 0.001:
            self.seek(0.0)

        try:
            self._ensure_default_output_configuration()

            info = sd.query_devices(self.output_device, "output")
            channels = int(info.get("max_output_channels", 0))
            default_rate = float(info.get("default_samplerate", 48000.0))
            desired_rate = self._choose_output_samplerate(
                channels,
                default_rate,
            )

            stream_needs_open = (
                self._stream is None
                or self._stream_device != self.output_device
                or int(self._stream_samplerate) != int(desired_rate)
                or self._opened_output_config_revision != self._output_config_revision
            )

            if stream_needs_open:
                self._open_stream(desired_rate)
            elif not self._playback_pipeline_matches(desired_rate):
                self._prepare_playback_audio(desired_rate)

        except Exception as exc:
            self.error.emit(f"Audio Output: {exc}")
            self.status_changed.emit("AUDIO ERROR")
            return

        with self._lock:
            source = self._playback_audio
            frame = int(self._position_frames)
            rate = int(self._playback_rate)

        if source is None:
            self.error.emit("Streaming source chưa sẵn sàng.")
            return

        source.notify_position(frame)
        if not source.ready(frame):
            with self._lock:
                self.running = False
                self._play_when_buffer_ready = True
            ahead = source.buffered_seconds_ahead(frame)
            self.status_changed.emit(
                f"BUFFERING — {ahead:.1f}s / {self._stream_startup_seconds:.0f}s"
            )
            return

        with self._lock:
            self._play_when_buffer_ready = False
            self.running = True

        self.status_changed.emit(f"PLAYING — STREAM {rate} HZ")

    def pause(self) -> None:
        with self._lock:
            self.running = False
            self._play_when_buffer_ready = False
            self._position_revision += 1

        self._reset_ltc_state()
        self.status_changed.emit("PAUSED — LTC OFF")

    def stop(self) -> None:
        with self._lock:
            self.running = False
            self._play_when_buffer_ready = False
            self.position_seconds = 0.0
            self._position_frames = 0
            source = self._playback_audio
            rate = int(self._playback_rate or self.audio_rate or 48000)
            self._position_revision += 1

        if source is not None:
            # Re-buffer from zero so the next GO has a deterministic startup
            # window instead of depending on stale chunks near the old stop.
            source.start(rate, 0.0)
            source.notify_position(0)

        self._reset_ltc_state()
        self.status_changed.emit("STOPPED — LTC OFF")

    def restart_outputs(self) -> None:
        was_playing = self.running

        with self._lock:
            self.running = False

        try:
            self._open_stream()
        except Exception as exc:
            self.error.emit(f"Audio Output: {exc}")
            self.status_changed.emit("AUDIO ERROR")
            return

        if was_playing:
            with self._lock:
                self.running = True
            self.status_changed.emit("PLAYING")
        else:
            self.status_changed.emit("OUTPUTS APPLIED")

    def close_streams(self) -> None:
        """Close every output stream but keep the loaded track.

        A device rescan has to terminate PortAudio, which is only safe once no
        stream is open. close() would also throw away the decoded decks, so a
        rescan uses this and reopens the outputs afterwards.
        """
        with self._lock:
            self.running = False

        self._close_extra_output_streams()

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        self._stream_samplerate = 0
        self._stream_device = None
        self._opened_output_config_revision = -1

    def close(
        self,
    ) -> None:
        self.close_streams()

        sources = []
        with self._lock:
            for source in self.deck_audio:
                if source is not None and source not in sources:
                    sources.append(source)
            self.deck_audio = [None, None]
            self.audio = None
            self._playback_audio = None
        for source in sources:
            try:
                source.close()
            except Exception:
                pass
