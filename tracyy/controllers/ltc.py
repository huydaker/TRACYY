from __future__ import annotations

import math
import time
from dataclasses import dataclass

import sounddevice as sd
import soundfile as sf
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView

from tracyy.core import RateLimiter, seconds_to_clock
from tracyy.core.reactive import set_line_text, set_text, set_tooltip, set_value
from tracyy.ltc.input import LTCFrame, compensated_total_frames, select_latest_offset_group
from tracyy.widgets import frames_to_timecode, timecode_to_frames


@dataclass(frozen=True, slots=True)
class _TimelineState:
    """One sample of the timeline, shared by every refresh tier in a pass."""

    chase_enabled: bool
    position: float
    duration: float
    project_fps: int
    fps: int
    timecode_position: float
    timecode: str
    active_path: str
    progress: float
    running: bool


class LTCControllerMixin:
    #: Chase lock state is re-read at most this often; it walks the input
    #: frame history, which is far too expensive for the playhead tier.
    _input_lock_state_limiter: RateLimiter | None = None
    _timeline_state_cache: _TimelineState | None = None
    _timeline_state_sampled_at: float = 0.0

    def input_timecode_chase_enabled(self) -> bool:
        return bool(
            getattr(self, "_input_timecode_enabled", False)
            and hasattr(self, "input_timecode_enable")
            and self.input_timecode_enable.isChecked()
        )

    def input_timecode_signal_state(
        self,
    ) -> str:
        return str(
            getattr(
                self,
                "_input_timecode_signal_state",
                "OFF",
            )
            or "OFF"
        ).upper()

    def input_timecode_has_live_chase(
        self,
    ) -> bool:
        return bool(
            self.input_timecode_chase_enabled()
            and self._input_timecode_locked
            and self.input_timecode_signal_state()
            in {
                "LOCKED",
                "AFTER_ROLL",
            }
        )

    def reset_input_timecode_validation(
        self,
        keep_anchor: bool = True,
    ) -> None:
        fps = self.input_timecode_selected_fps()
        self._input_timecode_input_fps = fps

        self._input_timecode_validator.configure(
            fps=fps,
            pre_roll_seconds=(self.input_timecode_pre_roll_seconds()),
            reset=True,
        )

        self._input_timecode_locked = False
        self._input_timecode_last_received_at = 0.0
        self._input_timecode_last_raw_received_at = 0.0

        if not keep_anchor:
            self._input_timecode_anchor_frames = 0
            self._input_timecode_anchor_seconds = 0.0

    def activate_input_timecode_preview_track(
        self,
        path: str,
    ) -> bool:
        """Load any playlist item for inspection while LTC is unavailable."""

        path = str(path or "").strip()
        if not path:
            return False

        row = self.playlist_row_for_path(path)
        if row < 0:
            return False

        self._input_timecode_active_path = path
        self._input_timecode_manual_preview = True
        self._input_timecode_match_paths = []
        self._input_timecode_match_signature = ()

        if not self.activate_playlist_row(
            row,
            autoplay=False,
        ):
            return False

        self.transport.pause()
        self.transport.seek(0.0)

        self._input_timecode_active_path = path
        self._input_timecode_manual_preview = True

        display_title = self.playlist_display_title_for_path(path)
        self.now_playing.setText(f"PREVIEW: {display_title}")
        self.set_status(f"INPUT TC WAITING — PREVIEW {display_title}")

        self.refresh_input_timecode_playlist_highlights()
        self.refresh_position()
        return True

    def refresh_input_timecode_devices(self) -> None:
        if not hasattr(self, "input_timecode_device"):
            return

        selected = self.input_timecode_device.currentData()
        self.input_timecode_device.blockSignals(True)
        self.input_timecode_device.clear()

        try:
            default_input = sd.default.device[0]
            default_index = (
                int(default_input)
                if default_input is not None and int(default_input) >= 0
                else None
            )

            for index, device in enumerate(sd.query_devices()):
                channels = int(device.get("max_input_channels", 0))
                if channels <= 0:
                    continue
                name = str(device.get("name", f"Device {index}"))
                label = f"{name} — {channels} inputs"
                if index == default_index:
                    label += "  [System Default]"
                self.input_timecode_device.addItem(label, index)

            target = selected if selected is not None else default_index
            found = self.input_timecode_device.findData(target)
            if found < 0 and self.input_timecode_device.count() > 0:
                found = 0
            if found >= 0:
                self.input_timecode_device.setCurrentIndex(found)
        except Exception as exc:
            self.on_input_timecode_runtime_status(f"INPUT DEVICE ERROR — {exc}")
        finally:
            self.input_timecode_device.blockSignals(False)

        self.rebuild_input_timecode_channels()

    def rebuild_input_timecode_channels(self) -> None:
        if not hasattr(self, "input_timecode_channel"):
            return

        previous = self.input_timecode_channel.currentData()
        self.input_timecode_channel.blockSignals(True)
        self.input_timecode_channel.clear()

        device_index = self.input_timecode_device.currentData()
        channel_count = 0
        if device_index is not None:
            try:
                info = sd.query_devices(int(device_index), "input")
                channel_count = int(info.get("max_input_channels", 0))
            except Exception:
                channel_count = 0

        for channel in range(channel_count):
            self.input_timecode_channel.addItem(
                f"Channel {channel + 1}",
                channel,
            )

        found = self.input_timecode_channel.findData(previous)
        if found < 0 and self.input_timecode_channel.count() > 0:
            found = 0
        if found >= 0:
            self.input_timecode_channel.setCurrentIndex(found)

        self.input_timecode_channel.blockSignals(False)

    def on_input_timecode_device_changed(self, _index: int) -> None:
        self.rebuild_input_timecode_channels()
        if self.input_timecode_chase_enabled():
            self.start_input_timecode_engine()

    def on_input_timecode_channel_changed(self, _index: int) -> None:
        if self.input_timecode_chase_enabled():
            self.start_input_timecode_engine()

    def on_input_timecode_fps_changed(
        self,
        value: str,
    ) -> None:
        """Handle Project/Output FPS only.

        Input LTC decoding uses the separate Input FPS control.
        """

        try:
            new_fps = int(value)
        except Exception:
            return

        old_fps = max(
            1,
            int(
                getattr(
                    self,
                    "_input_timecode_project_fps",
                    new_fps,
                )
            ),
        )

        if new_fps == old_fps:
            return

        migrated: dict[str, int] = {}

        for path, old_frames in self.track_offsets.items():
            seconds = (
                max(
                    0,
                    int(old_frames),
                )
                / old_fps
            )

            migrated[str(path)] = round(seconds * new_fps)

        self.track_offsets = migrated
        self.save_track_offsets()
        self._input_timecode_project_fps = new_fps
        self._input_timecode_duration_cache.clear()

        self.update_main_offset_ui()
        self.sync_cue_time_editor_from_selection()
        self.refresh_cue_table()
        self.refresh_position()

    def set_input_timecode_controls_enabled(self, chase_enabled: bool) -> None:
        for widget in (
            self.play_pause_button,
            self.stop_button,
            self.zero_button,
            self.previous_button,
            self.next_button,
            self.position,
        ):
            widget.setEnabled(not chase_enabled)

        # Dragging the waveform playhead is the same kind of control as the
        # position slider above: while chasing, the incoming timecode owns it.
        self.main_waveform.scrub_enabled = not chase_enabled

    def input_timecode_selected_fps(
        self,
    ) -> int:
        control = getattr(
            self,
            "input_timecode_fps",
            None,
        )

        if control is None:
            return int(self._input_timecode_fps_default)

        try:
            fps = int(control.currentText())
        except Exception:
            fps = int(self._input_timecode_fps_default)

        if fps not in {
            24,
            25,
            30,
        }:
            fps = int(self._input_timecode_fps_default)

        return fps

    def input_timecode_latency_ms_value(
        self,
    ) -> float:
        control = getattr(
            self,
            "input_timecode_latency_ms",
            None,
        )

        if control is None:
            return float(self._input_timecode_latency_ms_default)

        return float(control.value())

    def input_timecode_latency_frames(
        self,
        fps: int | None = None,
    ) -> int:
        rate = (
            self.input_timecode_selected_fps()
            if fps is None
            else max(
                1,
                int(fps),
            )
        )

        return round(self.input_timecode_latency_ms_value() * rate / 1000.0)

    def on_input_timecode_latency_changed(
        self,
        _value: float | None = None,
    ) -> None:
        fps = self.input_timecode_selected_fps()
        frames = self.input_timecode_latency_frames(fps)
        latency_ms = self.input_timecode_latency_ms_value()

        if self.input_timecode_chase_enabled():
            self.on_input_timecode_runtime_status(
                f"INPUT LATENCY {latency_ms:+.1f} ms ({frames:+d}F @ {fps} FPS)"
            )

        if hasattr(
            self,
            "mark_project_dirty",
        ):
            self.mark_project_dirty()

    def on_input_timecode_input_fps_changed(
        self,
        value: str,
    ) -> None:
        try:
            new_fps = int(value)
        except Exception:
            return

        if new_fps not in {
            24,
            25,
            30,
        }:
            return

        held_seconds = self.input_timecode_current_seconds()

        self._input_timecode_input_fps = new_fps
        self._input_timecode_anchor_seconds = held_seconds
        self._input_timecode_anchor_frames = math.floor(held_seconds * new_fps)

        self._input_timecode_validator.configure(
            fps=new_fps,
            pre_roll_seconds=(self.input_timecode_pre_roll_seconds()),
            reset=True,
        )

        if self.input_timecode_chase_enabled():
            self._input_timecode_locked = False
            self._input_timecode_signal_state = "WAITING"
            self.stop_input_timecode_engine()

            if self.start_input_timecode_engine():
                self.on_input_timecode_runtime_status(
                    f"WAITING FOR {new_fps} FPS LTC — HOLDING POSITION"
                )

        if hasattr(
            self,
            "mark_project_dirty",
        ):
            self.mark_project_dirty()

        self.refresh_position()

    def input_timecode_pre_roll_seconds(
        self,
    ) -> float:
        control = getattr(
            self,
            "input_timecode_pre_roll",
            None,
        )
        if control is None:
            return float(self._input_timecode_pre_roll_default)
        return max(
            0.0,
            float(control.value()),
        )

    def input_timecode_after_roll_seconds(
        self,
    ) -> float:
        control = getattr(
            self,
            "input_timecode_after_roll",
            None,
        )
        if control is None:
            return float(self._input_timecode_after_roll_default)
        return max(
            0.0,
            float(control.value()),
        )

    def on_input_timecode_roll_changed(
        self,
        _value: float | None = None,
    ) -> None:
        after_roll = self.input_timecode_after_roll_seconds()

        self._input_timecode_lost_timeout = max(
            0.05,
            after_roll,
        )

        input_fps = self.input_timecode_selected_fps()
        self._input_timecode_input_fps = input_fps

        self._input_timecode_validator.configure(
            fps=input_fps,
            pre_roll_seconds=(self.input_timecode_pre_roll_seconds()),
            reset=(
                self.input_timecode_signal_state()
                in {
                    "WAITING",
                    "PRE_ROLL",
                    "LOST",
                }
            ),
        )

        if self.input_timecode_chase_enabled() and not self._input_timecode_locked:
            self._input_timecode_signal_state = "WAITING"
            self.on_input_timecode_runtime_status("WAITING FOR LTC INPUT")

        if hasattr(
            self,
            "mark_project_dirty",
        ):
            self.mark_project_dirty()

    def set_input_timecode_enabled(
        self,
        enabled: bool,
    ) -> None:
        enabled = bool(enabled)

        held_media_position = 0.0
        if not enabled and self._input_timecode_active_path:
            held_media_position = self.current_timeline_media_position()

        current_file = str(self.transport.current_file or "").strip()

        self._input_timecode_enabled = enabled

        if enabled:
            self.transport.pause()
            self._input_timecode_signal_state = "WAITING"
            self._input_timecode_match_paths = []
            self._input_timecode_match_signature = ()

            # Keep an already loaded song available as a manual preview instead
            # of blanking the timeline merely because LTC has not arrived yet.
            self._input_timecode_active_path = current_file if current_file else ""
            self._input_timecode_manual_preview = bool(current_file)

            self.reset_input_timecode_validation(
                keep_anchor=False,
            )
            self.set_input_timecode_controls_enabled(True)
            self.refresh_input_timecode_playlist_highlights()

            if not self.start_input_timecode_engine():
                self._input_timecode_enabled = False
                self._input_timecode_signal_state = "OFF"
                self.input_timecode_enable.blockSignals(True)
                self.input_timecode_enable.setChecked(False)
                self.input_timecode_enable.blockSignals(False)
                self.set_input_timecode_controls_enabled(False)
                return

            self.on_input_timecode_runtime_status("WAITING FOR LTC INPUT")

            if current_file:
                self.set_status("INPUT TIMECODE CHASE — WAITING — TRACK PREVIEW AVAILABLE")
            else:
                self.set_status("INPUT TIMECODE CHASE — WAITING")

        else:
            self.stop_input_timecode_engine()

            if self.transport.current_file:
                self.transport.seek(held_media_position)

            self._input_timecode_locked = False
            self._input_timecode_signal_state = "OFF"
            self._input_timecode_manual_preview = False
            self._input_timecode_active_path = ""
            self._input_timecode_match_paths = []
            self._input_timecode_match_signature = ()

            self._input_timecode_validator.reset()

            self.set_input_timecode_controls_enabled(False)
            self.refresh_input_timecode_playlist_highlights()
            self.on_input_timecode_runtime_status("INPUT TIMECODE OFF")
            self.set_status("INPUT TIMECODE CHASE OFF")
            self.refresh_position()

        self.update_play_pause_icon()

    def start_input_timecode_engine(
        self,
    ) -> bool:
        engine = getattr(
            self,
            "_input_timecode_engine",
            None,
        )
        if engine is None:
            return False

        device_index = self.input_timecode_device.currentData()
        channel_index = self.input_timecode_channel.currentData()

        if device_index is None or channel_index is None:
            self.show_error("No LTC audio input device/channel is available.")
            return False

        try:
            input_fps = self.input_timecode_selected_fps()

            self._input_timecode_input_fps = input_fps
            self._input_timecode_validator.configure(
                fps=input_fps,
                pre_roll_seconds=(self.input_timecode_pre_roll_seconds()),
                reset=True,
            )

            self._input_timecode_locked = False
            self._input_timecode_signal_state = "WAITING"
            self._input_timecode_last_received_at = 0.0
            self._input_timecode_last_raw_received_at = 0.0

            engine.start(
                int(device_index),
                int(channel_index),
                input_fps,
            )
            return True

        except Exception as exc:
            self.on_input_timecode_runtime_status(f"LTC INPUT ERROR — {exc}")
            self.show_error(str(exc))
            return False

    def stop_input_timecode_engine(self) -> None:
        engine = getattr(self, "_input_timecode_engine", None)
        if engine is not None:
            engine.stop()

    def on_input_timecode_runtime_status(
        self,
        message: str,
    ) -> None:
        message = str(message or "").strip()

        if not message or not hasattr(
            self,
            "input_timecode_status_label",
        ):
            return

        self._input_timecode_last_status = message
        upper = message.upper()

        if "LOCKED" in upper and "UNLOCKED" not in upper:
            state = "locked"

        elif "ERROR" in upper or "LOST" in upper:
            state = "error"

        elif (
            "WAIT" in upper
            or "STARTED" in upper
            or "PRE-ROLL" in upper
            or "AFTER-ROLL" in upper
            or "VERIFY" in upper
        ):
            state = "waiting"

        else:
            state = "off"

        self.input_timecode_status_label.setText(message)
        self.input_timecode_status_label.setProperty(
            "state",
            state,
        )
        self.input_timecode_status_label.style().unpolish(self.input_timecode_status_label)
        self.input_timecode_status_label.style().polish(self.input_timecode_status_label)

    def on_input_timecode_frame(
        self,
        frame: object,
    ) -> None:
        if not self.input_timecode_chase_enabled():
            return

        try:
            fps = int(frame.fps)
            received = float(
                getattr(
                    frame,
                    "received_monotonic",
                    time.monotonic(),
                )
            )
            captured = float(
                getattr(
                    frame,
                    "captured_monotonic",
                    0.0,
                )
                or 0.0
            )
            source = str(
                getattr(
                    frame,
                    "source",
                    "LTC_AUDIO",
                )
            )
            raw_frames = int(frame.total_frames)
            manual_latency_frames = self.input_timecode_latency_frames(fps)
            effective_frames = compensated_total_frames(
                frame,
                extra_compensation_frames=(manual_latency_frames),
            )
            delivery_latency = max(
                0.0,
                float(
                    getattr(
                        frame,
                        "delivery_latency_seconds",
                        0.0,
                    )
                    or 0.0
                ),
            )
        except Exception:
            return

        if fps not in {
            24,
            25,
            30,
        }:
            return

        selected_input_fps = self.input_timecode_selected_fps()

        # Ignore stale callbacks emitted by the previous decoder after the
        # operator changes Input FPS.
        if fps != selected_input_fps:
            return

        if fps != self._input_timecode_input_fps:
            self._input_timecode_input_fps = fps
            self._input_timecode_validator.configure(
                fps=fps,
                pre_roll_seconds=(self.input_timecode_pre_roll_seconds()),
                reset=True,
            )

        validation_time = captured if captured > 0.0 else received

        self._input_timecode_last_raw_received_at = received

        result = self._input_timecode_validator.process(
            raw_total_frames=raw_frames,
            effective_total_frames=(effective_frames),
            timestamp=validation_time,
            source=source,
        )

        if not result.accepted:
            if result.status in {
                "PREROLL",
                "PREROLL_RESET",
            }:
                self._input_timecode_locked = False
                self._input_timecode_signal_state = "PRE_ROLL"

                pre_roll = self.input_timecode_pre_roll_seconds()
                elapsed = result.progress * pre_roll

                self.on_input_timecode_runtime_status(
                    f"LTC PRE-ROLL — {elapsed:.2f} / {pre_roll:.2f} s"
                )
                self.set_status("INPUT TIMECODE PRE-ROLL")

            elif result.status == "SEEK_CANDIDATE":
                # Keep Last Good TC, track, playhead and cues untouched while the
                # discontinuity is being verified.
                self.on_input_timecode_runtime_status(
                    f"LTC SEEK VERIFY — {result.candidate_count}/{result.required_count}"
                )

            elif result.status == "BACKWARD_JITTER":
                # One small backward decoder error must not move the timeline.
                self.on_input_timecode_runtime_status("LTC LOCKED — BACKWARD JITTER IGNORED")

            return

        self._input_timecode_input_fps = fps
        self._input_timecode_anchor_frames = result.effective_total_frames
        self._input_timecode_anchor_seconds = result.effective_total_frames / float(fps)
        self._input_timecode_last_received_at = received
        self._input_timecode_delivery_latency = delivery_latency
        self._input_timecode_phase_frames = 1 if source.upper() == "LTC_AUDIO" else 0

        was_live = self.input_timecode_has_live_chase()

        self._input_timecode_locked = True
        self._input_timecode_signal_state = "LOCKED"
        self._input_timecode_manual_preview = False

        effective_tc = frames_to_timecode(
            result.effective_total_frames,
            fps,
        )
        self.input_timecode_edit.setText(effective_tc)

        latency_frames = round(delivery_latency * fps)

        suffix = " — SEEK CONFIRMED" if result.confirmed_seek else ""

        manual_latency_frames = self.input_timecode_latency_frames(fps)
        manual_latency_ms = self.input_timecode_latency_ms_value()

        self.on_input_timecode_runtime_status(
            f"LTC LOCKED — {effective_tc} — "
            f"{fps} FPS — ADC +{latency_frames}F — "
            f"MANUAL {manual_latency_ms:+.1f}ms/"
            f"{manual_latency_frames:+d}F"
            f"{suffix}"
        )

        self.update_input_timecode_track_matching(
            timeline_seconds=(self._input_timecode_anchor_seconds),
            force=(not was_live or result.confirmed_seek),
        )

    def apply_input_timecode(
        self,
    ) -> None:
        if not self.input_timecode_chase_enabled():
            self.show_error("Enable Input Timecode Chase before injecting a test frame.")
            return

        try:
            fps = self.input_timecode_selected_fps()
            value = self.input_timecode_edit.text().strip()
            total_frames = timecode_to_frames(
                value,
                fps,
            )
            normalized = frames_to_timecode(
                total_frames,
                fps,
            )
            (
                hh,
                mm,
                ss,
                ff,
            ) = map(
                int,
                normalized.split(":"),
            )

            self.on_input_timecode_frame(
                LTCFrame(
                    hours=hh,
                    minutes=mm,
                    seconds=ss,
                    frames=ff,
                    fps=fps,
                    total_frames=total_frames,
                    timecode=normalized,
                    received_monotonic=(time.monotonic()),
                    source="MANUAL_TEST",
                )
            )

        except Exception as exc:
            self.show_error(str(exc))

    def input_timecode_current_seconds(
        self,
    ) -> float:
        anchor = float(self._input_timecode_anchor_seconds)

        if not self._input_timecode_locked:
            return anchor

        elapsed = max(
            0.0,
            time.monotonic() - self._input_timecode_last_received_at,
        )

        # During AFTER-ROLL the timeline free-runs only for the configured period.
        # It never falls back to zero.
        return (
            anchor
            + min(
                elapsed,
                self.input_timecode_after_roll_seconds(),
            )
        ) % (24.0 * 60.0 * 60.0)

    def refresh_input_timecode_lock_state(
        self,
    ) -> None:
        if not self.input_timecode_chase_enabled():
            return

        now = time.monotonic()
        state = self.input_timecode_signal_state()

        # Initial/pre-roll signal disappeared before lock: reset pre-roll and keep
        # any manually selected preview track visible.
        if not self._input_timecode_locked:
            if state == "PRE_ROLL":
                raw_age = now - self._input_timecode_last_raw_received_at

                frame_gap = max(
                    0.12,
                    3.0
                    / max(
                        1,
                        self._input_timecode_input_fps,
                    ),
                )

                if raw_age > frame_gap:
                    self._input_timecode_validator.reset_preroll_only()
                    self._input_timecode_signal_state = "WAITING"
                    self.on_input_timecode_runtime_status("WAITING FOR LTC INPUT")

            return

        age = max(
            0.0,
            now - self._input_timecode_last_received_at,
        )

        frame_gap = max(
            0.08,
            2.5
            / max(
                1,
                self._input_timecode_input_fps,
            ),
        )

        if age <= frame_gap:
            if state != "LOCKED":
                self._input_timecode_signal_state = "LOCKED"
            return

        after_roll = self.input_timecode_after_roll_seconds()

        if age <= after_roll:
            self._input_timecode_signal_state = "AFTER_ROLL"

            self.on_input_timecode_runtime_status(
                f"LTC AFTER-ROLL — {age:.2f} / {after_roll:.2f} s"
            )
            return

        held_seconds = self.input_timecode_current_seconds()
        fps = max(
            1,
            int(
                getattr(
                    self,
                    "_input_timecode_input_fps",
                    30,
                )
            ),
        )

        self._input_timecode_anchor_seconds = held_seconds
        self._input_timecode_anchor_frames = math.floor(held_seconds * fps)
        self._input_timecode_locked = False
        self._input_timecode_signal_state = "LOST"

        self.on_input_timecode_runtime_status("INPUT TIMECODE LOST — HOLDING LAST GOOD FRAME")
        self.set_status("INPUT TIMECODE LOST")

    def input_timecode_track_duration(self, path: str) -> float:
        normalized = str(path)
        cached = float(self._input_timecode_duration_cache.get(normalized, 0.0))
        if cached > 0.0:
            return cached

        if normalized == str(self.transport.current_file or ""):
            duration = float(self.transport.duration or 0.0)
            if duration > 0.0:
                self._input_timecode_duration_cache[normalized] = duration
                return duration

        cached_audio = self.transport.cached_audio(normalized)
        if cached_audio is not None:
            audio, rate = cached_audio
            duration = len(audio) / float(rate) if rate > 0 else 0.0
            if duration > 0.0:
                self._input_timecode_duration_cache[normalized] = duration
                return duration

        try:
            info = sf.info(normalized)
            duration = info.frames / float(info.samplerate) if info.samplerate > 0 else 0.0
        except Exception:
            duration = 0.0

        if duration > 0.0:
            self._input_timecode_duration_cache[normalized] = duration
        return duration

    def input_timecode_track_entries(self) -> list[tuple[str, float, float]]:
        raw_entries: list[tuple[str, float, float]] = []

        for row in range(self.playlist.count()):
            item = self.playlist.item(row)
            if item is None:
                continue
            path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not path:
                continue

            offset_seconds = self.track_offset_seconds(path)
            duration_seconds = self.input_timecode_track_duration(path)
            raw_entries.append((path, offset_seconds, duration_seconds))

        offsets = sorted({offset for _path, offset, _duration in raw_entries})
        day_seconds = 24.0 * 60.0 * 60.0
        entries: list[tuple[str, float, float]] = []

        for path, offset, duration in raw_entries:
            if duration <= 0.0:
                next_offsets = [value for value in offsets if value > offset]
                next_offset = next_offsets[0] if next_offsets else day_seconds
                duration = max(0.001, next_offset - offset)
            entries.append((path, offset, duration))

        return entries

    def update_input_timecode_track_matching(
        self,
        timeline_seconds: float | None = None,
        force: bool = False,
    ) -> None:
        if not self.input_timecode_chase_enabled():
            return

        # No validated LTC means no automatic track matching. A manually selected
        # song remains loaded and visible on the timeline.
        if self.input_timecode_signal_state() != "LOCKED":
            return

        if timeline_seconds is None:
            timeline_seconds = self.input_timecode_current_seconds()

        matches = select_latest_offset_group(
            self.input_timecode_track_entries(),
            float(timeline_seconds),
            pre_roll_seconds=0.0,
            after_roll_seconds=0.0,
        )
        signature = tuple(matches)

        self._input_timecode_match_paths = list(matches)
        self.refresh_input_timecode_playlist_highlights()

        active = str(self._input_timecode_active_path or "")

        if active and active not in matches:
            self._input_timecode_active_path = ""
            active = ""
            self.refresh_input_timecode_playlist_highlights()

        if len(matches) == 1:
            only_path = matches[0]

            if active != only_path:
                self.activate_input_timecode_track(
                    only_path,
                    operator_selected=False,
                )

        elif len(matches) > 1:
            if active in matches:
                self._input_timecode_manual_preview = False
                self.refresh_input_timecode_playlist_highlights()

            elif signature != self._input_timecode_match_signature or force:
                self.playlist.blockSignals(True)
                self.playlist.clearSelection()
                self.playlist.setCurrentRow(-1)
                self.playlist.blockSignals(False)

                project_fps = max(
                    1,
                    int(self.fps.currentText()),
                )
                offset_tc = frames_to_timecode(
                    self.track_offset_frames(matches[0]),
                    project_fps,
                )

                self.set_status(f"{len(matches)} TRACKS MATCH {offset_tc} — CLICK ONE TRACK")

        else:
            if signature != self._input_timecode_match_signature or force:
                self.playlist.blockSignals(True)
                self.playlist.clearSelection()
                self.playlist.setCurrentRow(-1)
                self.playlist.blockSignals(False)
                self.set_status("WAITING FOR A TRACK OFFSET MATCH")

        self._input_timecode_match_signature = signature

    def refresh_input_timecode_playlist_highlights(self) -> None:
        if not hasattr(self, "playlist"):
            return

        matches = set(self._input_timecode_match_paths)
        active = str(self._input_timecode_active_path or "")

        for row in range(self.playlist.count()):
            item = self.playlist.item(row)
            if item is None:
                continue
            path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if self.input_timecode_chase_enabled() and path in matches:
                item.setData(
                    Qt.ItemDataRole.BackgroundRole,
                    QColor("#343A32"),
                )
            else:
                item.setData(Qt.ItemDataRole.BackgroundRole, None)

        if self.input_timecode_chase_enabled() and active:
            row = self.playlist_row_for_path(active)
            if row >= 0:
                self.playlist.blockSignals(True)
                self.playlist.setCurrentRow(row)
                current_item = self.playlist.item(row)
                if current_item is not None:
                    self.playlist.scrollToItem(
                        current_item,
                        QAbstractItemView.ScrollHint.PositionAtCenter,
                    )
                self.playlist.blockSignals(False)

    def activate_input_timecode_track(
        self,
        path: str,
        operator_selected: bool,
    ) -> bool:
        path = str(path or "").strip()

        if not path or path not in self._input_timecode_match_paths:
            return False

        row = self.playlist_row_for_path(path)
        if row < 0:
            return False

        try:
            self.transport.pause()

            if self.transport.current_file != path:
                self.transport.load_audio(path)

            if self.transport.current_file != path:
                raise RuntimeError("Unable to load the selected chase track.")

            with self.transport._lock:
                self.transport.timecode_offset_seconds = self.track_offset_seconds(path)
                self.transport._position_revision += 1

            self._input_timecode_active_path = path
            self._input_timecode_manual_preview = False
            self.playing_playlist_row = row
            self.standby_path = None
            self.standby_playlist_row = -1
            self.standby_panel.setVisible(False)

            self.playlist.blockSignals(True)
            self.playlist.setCurrentRow(row)
            self.playlist.blockSignals(False)

            self.load_cue_csv_if_available(path)
            self._active_playback_cue_id = None
            self._last_playhead_position = -1.0
            self.refresh_cue_table()

            self.main_waveform.clear_track()
            self._start_main_preview_load(path)
            self.update_main_offset_ui()
            self.sync_cue_time_editor_from_selection()

            media_position = self.current_timeline_media_position()
            self.transport.seek(media_position)

            display_title = self.playlist_display_title_for_path(path)
            self.now_playing.setText(f"CHASE: {display_title}")
            source = "OPERATOR" if operator_selected else "AUTO"
            self.set_status(f"INPUT TC {source} TRACK — {display_title}")
            self.refresh_input_timecode_playlist_highlights()
            self.refresh_position()
            return True

        except Exception as exc:
            self.show_error(str(exc))
            return False

    def current_timeline_media_position(
        self,
    ) -> float:
        if not self.input_timecode_chase_enabled():
            return self.transport.current_position()

        path = str(self._input_timecode_active_path or "").strip()

        if not path:
            return 0.0

        # While waiting, pre-rolling or lost, an operator may inspect any track.
        # The waveform and cues remain visible at the manually selected media
        # position instead of being forced back to an empty 00:00:00:00 timeline.
        if self._input_timecode_manual_preview and not self.input_timecode_has_live_chase():
            duration = float(self.transport.duration or 0.0)
            position = max(
                0.0,
                self.transport.current_position(),
            )

            if duration > 0.0:
                position = min(
                    duration,
                    position,
                )

            return position

        position = self.timecode_to_media_seconds(
            self.input_timecode_current_seconds(),
            path,
        )
        duration = float(self.transport.duration or 0.0)

        if duration > 0.0:
            position = min(
                duration,
                position,
            )

        return max(
            0.0,
            position,
        )

    def begin_timecode_edit(
        self,
        event=None,
    ) -> None:
        fps = int(self.fps.currentText())
        current = frames_to_timecode(
            int(self.transport.current_position() * fps),
            fps,
        )
        self.tc_editor.setText(current)
        self.tc_stack.setCurrentWidget(self.tc_editor)
        self.tc_editor.show()
        self.tc_editor.setFocus()
        self.tc_editor.selectAll()

    def commit_timecode_edit(self) -> None:
        if self.tc_stack.currentWidget() is not self.tc_editor:
            return

        try:
            fps = int(self.fps.currentText())
            value = self.tc_editor.text().strip()
            frame = timecode_to_frames(value, fps)
            self.transport.seek(frame / fps)
            self.tc_label.setText(frames_to_timecode(frame, fps))
        except Exception as exc:
            self.show_error(str(exc))
        finally:
            self.tc_stack.setCurrentWidget(self.tc_label)
            self.tc_editor.hide()

    # -- periodic refresh -------------------------------------------------
    #
    # V1 ran this whole body from a 15 ms PreciseTimer: fifteen unconditional
    # setText calls, a rebuilt server snapshot and the cue lookups, 66 times a
    # second, whether or not the transport was running and whether or not the
    # window was even on screen. V2 splits it into tiers that
    # :class:`~tracyy.core.perf.UiTickScheduler` paces independently, and every
    # write is guarded by a comparison, so an idle window costs almost nothing
    # and a playing one only repaints what actually changed.

    def _timeline_state(self, *, force: bool = False) -> _TimelineState:
        """Sample the timeline once per dispatch pass.

        The scheduler can run several tiers in the same pass; without this the
        position would be sampled — and the smoothing filter advanced — once
        per tier.
        """
        now = time.perf_counter()
        cached = getattr(self, "_timeline_state_cache", None)
        if not force and cached is not None and now - self._timeline_state_sampled_at < 0.004:
            return cached

        chase_enabled = self.input_timecode_chase_enabled()
        if chase_enabled:
            limiter = self._input_lock_state_limiter
            if limiter is None:
                limiter = self._input_lock_state_limiter = RateLimiter(0.1)
            if limiter.ready(now=now):
                self.refresh_input_timecode_lock_state()
            position = self.current_timeline_media_position()
            duration = float(self.transport.duration or 0.0) if self.main_track_path() else 0.0
        else:
            # Interpolated between audio callbacks. The raw transport position
            # only moves once per audio block, which makes the playhead advance
            # in visible steps at this refresh rate.
            position = self.transport.smoothed_position()
            duration = self.transport.duration

        project_fps = int(self.fps.currentText())
        fps = max(1, int(self._input_timecode_input_fps)) if chase_enabled else project_fps
        timecode_position = (
            self.input_timecode_current_seconds()
            if chase_enabled
            else self.media_to_timecode_seconds(position)
        )
        active_path = self.main_track_path()

        state = _TimelineState(
            chase_enabled=chase_enabled,
            position=position,
            duration=duration,
            project_fps=project_fps,
            fps=fps,
            timecode_position=timecode_position,
            timecode=frames_to_timecode(math.floor(timecode_position * fps + 1e-9), fps),
            active_path=active_path,
            progress=(position / duration) if duration > 0 else 0.0,
            running=bool(
                self.transport.running
                or (chase_enabled and self._input_timecode_locked and active_path)
            ),
        )
        self._timeline_state_cache = state
        self._timeline_state_sampled_at = now
        return state

    def tick_playhead(self) -> None:
        """Highest-rate tier: everything a viewer would see stutter."""
        state = self._timeline_state()

        set_text(self.tc_label, state.timecode)
        if state.chase_enabled:
            if self.transport_elapsed_label.minimumWidth() != 105:
                self.transport_elapsed_label.setMinimumWidth(105)
            set_text(self.transport_elapsed_label, state.timecode)
        else:
            if self.transport_elapsed_label.minimumWidth() != 56:
                self.transport_elapsed_label.setMinimumWidth(56)
            set_text(self.transport_elapsed_label, seconds_to_clock(state.position))

        if not self._seeking and state.duration:
            set_value(self.position, int(state.progress * self.position.maximum()))

        self.main_waveform.set_progress(state.progress, playing=state.running)

        if state.active_path and self._cue_runtime_enabled:
            self.update_cue_selection_from_playhead(state.position)
        if state.active_path and self._cue_countdown_enabled:
            self.update_next_cue_loading(state.position)

    def tick_labels(self) -> None:
        """Text that changes at most a few times a second."""
        state = self._timeline_state()

        set_text(
            self.clock_label,
            f"{seconds_to_clock(state.position)} / {seconds_to_clock(state.duration)}",
        )
        set_text(self.transport_duration_label, seconds_to_clock(state.duration))

        title = (
            self.playlist_display_title_for_path(state.active_path)
            if state.active_path
            else "NO TRACK"
        )
        set_text(self.transport_title_label, title.upper())
        set_tooltip(self.transport_title_label, state.active_path or "")

        end_tc = frames_to_timecode(
            round(
                self.media_to_timecode_seconds(state.duration, state.active_path)
                * state.project_fps
            ),
            state.project_fps,
        )
        set_line_text(self.cue_time_editor, f"{state.timecode} / {end_tc}")

        self.update_play_pause_icon()
        self.refresh_playlist_row_states()

        if state.chase_enabled and self.input_timecode_signal_state() == "LOCKED":
            self.update_input_timecode_track_matching(
                timeline_seconds=state.timecode_position,
            )

    def tick_network(self) -> None:
        """Publish to the web cue viewer and any follow machines."""
        state = self._timeline_state()
        self.update_server_snapshot(
            state.position,
            state.duration,
            state.fps,
            state.timecode,
        )

    def refresh_position(self) -> None:
        """Run every tier now.

        Called after a seek, a load or a settings change, where waiting for the
        next scheduled tick would show stale numbers.
        """
        self._timeline_state(force=True)
        self.tick_playhead()
        self.tick_labels()
        self.tick_network()
