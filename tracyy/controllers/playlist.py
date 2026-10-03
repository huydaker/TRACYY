from __future__ import annotations

import csv
import io
import threading
import time
from concurrent.futures import FIRST_COMPLETED, wait
from pathlib import Path

import numpy as np
import soundfile as sf
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QTableWidgetItem,
)

from tracyy.core import seconds_to_clock, silent_message_box
from tracyy.widgets import (
    AUDIO_EXTENSIONS,
    PLAYLIST_MEDIA_META_ROLE,
    PLAYLIST_MISSING_ROLE,
    PLAYLIST_PROGRESS_ROLE,
    PLAYLIST_TITLE_ROLE,
    frames_to_timecode,
    timecode_to_frames,
)


class PlaylistControllerMixin:
    @staticmethod
    def format_ram_size(size_bytes: int) -> str:
        size = float(max(0, size_bytes))
        units = ("B", "KB", "MB", "GB", "TB")
        unit = units[0]

        for candidate in units:
            unit = candidate
            if size < 1024.0 or candidate == units[-1]:
                break
            size /= 1024.0

        if unit in {"B", "KB"}:
            return f"{size:.0f} {unit}"
        return f"{size:.2f} {unit}"

    def playlist_ram_summary(self) -> str:
        # V1.2.2 keeps only bounded Main/Standby PCM windows in memory.
        total = 0
        for source in getattr(self.transport, "deck_audio", []):
            if source is not None:
                total += int(getattr(source, "nbytes", 0) or 0)
        return self.format_ram_size(total)

    def normalized_audio_path(self, path: str) -> str:
        try:
            return str(Path(path).resolve())
        except Exception:
            return str(path)

    def is_audio_ready_in_ram(self, path: str) -> bool:
        # Legacy method name retained for controller compatibility. READY now
        # means the streaming startup buffer is ready, not full-song RAM.
        try:
            return bool(self.transport.is_buffer_ready(path))
        except Exception:
            return False

    def request_play_when_ram_ready(
        self,
        path: str,
        *,
        position: float | None = None,
    ) -> bool:
        normalized = self.normalized_audio_path(path)
        if self.transport.current_file == normalized:
            if position is not None:
                self.transport.seek(float(position))
            return bool(self.transport.is_buffer_ready(normalized))

        # Prepare only a bounded Standby buffer. Transport.play() itself owns
        # the wait-for-buffer behavior for Main.
        try:
            self.transport.preload_music(normalized)
        except Exception:
            return False
        return bool(self.transport.is_buffer_ready(normalized))

    def play_path_from_ram(
        self,
        path: str,
        *,
        position: float = 0.0,
    ) -> None:
        normalized = self.normalized_audio_path(path)
        if self.transport.current_file != normalized:
            self.transport.load_audio(normalized)
        if position > 0:
            self.transport.seek(position)
        self.transport.play()

    def queue_playlist_preload(
        self,
        paths: list[str] | None = None,
        *,
        prioritize: list[str] | None = None,
    ) -> None:
        if paths is None:
            paths = []
            for row in range(self.playlist.count()):
                item = self.playlist.item(row)
                if item is None:
                    continue
                path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
                if path:
                    paths.append(path)

        priority_paths = [
            str(Path(path).resolve())
            for path in (prioritize or [])
            if path and Path(path).exists()
        ]
        normal_paths = [
            str(Path(path).resolve()) for path in paths if path and Path(path).exists()
        ]

        ordered: list[str] = []
        for path in priority_paths + normal_paths:
            if path not in ordered:
                ordered.append(path)

        with self._playlist_preload_lock:
            for path in reversed(priority_paths):
                if path in self.standby_preview_cache or path in self._playlist_preload_pending:
                    continue
                self._playlist_preload_queue.insert(0, path)
                self._playlist_preload_pending.add(path)
                self._playlist_preload_state[path] = "QUEUED"
                QTimer.singleShot(
                    0,
                    lambda path=path: self.update_playlist_item_load_state(
                        path,
                        "QUEUED",
                    ),
                )

            for path in normal_paths:
                if path in self.standby_preview_cache or path in self._playlist_preload_pending:
                    continue
                self._playlist_preload_queue.append(path)
                self._playlist_preload_pending.add(path)
                self._playlist_preload_state[path] = "QUEUED"
                QTimer.singleShot(
                    0,
                    lambda path=path: self.update_playlist_item_load_state(
                        path,
                        "QUEUED",
                    ),
                )

            should_start = (
                bool(self._playlist_preload_queue) and not self._playlist_preload_running
            )
            if should_start:
                self._playlist_preload_running = True

        self.refresh_playlist_preload_tooltips()

        if should_start:
            threading.Thread(
                target=self._playlist_preload_worker,
                name="TracyyPlaylistPreload",
                daemon=True,
            ).start()

    def _playlist_preload_worker(self) -> None:
        """Analyze waveform/peaks in worker processes without loading audio RAM."""
        active: dict[object, str] = {}

        while True:
            running_audio = bool(getattr(self.transport, "running", False))
            parallel_limit = (
                1
                if running_audio
                else min(
                    2,
                    max(1, int(getattr(self.media_workers, "max_workers", 1))),
                )
            )

            while len(active) < parallel_limit:
                with self._playlist_preload_lock:
                    if not self._playlist_preload_queue:
                        path = ""
                    else:
                        path = self._playlist_preload_queue.pop(0)
                        self._playlist_preload_state[path] = "ANALYZING"

                if not path:
                    break

                try:
                    self.playlist_preload_progress.emit(path, 5)
                    future = self.media_workers.submit_waveform(path)
                    active[future] = path
                except Exception as exc:
                    self.playlist_preload_ready.emit(
                        path,
                        np.zeros(65536, dtype=np.float32),
                        False,
                        str(exc),
                    )

            if not active:
                with self._playlist_preload_lock:
                    if self._playlist_preload_queue:
                        continue
                    self._playlist_preload_running = False
                return

            done, _pending = wait(
                tuple(active.keys()),
                timeout=0.10,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                continue

            for future in done:
                path = active.pop(future, "")
                if not path:
                    continue
                try:
                    preview = np.ascontiguousarray(
                        np.asarray(future.result(), dtype=np.float32)
                    )
                    self.playlist_preload_progress.emit(path, 100)
                    self.playlist_preload_ready.emit(
                        path,
                        {"waveform": preview},
                        True,
                        "",
                    )
                except Exception as exc:
                    self.playlist_preload_ready.emit(
                        path,
                        np.zeros(65536, dtype=np.float32),
                        False,
                        str(exc),
                    )

    def on_transport_buffer_ready(self, path: str) -> None:
        normalized = self.normalized_audio_path(path)
        self._ram_ready_paths.add(normalized)  # legacy set; now means buffer ready

        if normalized == self.normalized_audio_path(self.standby_path or ""):
            self._standby_preloaded_path = normalized
            if getattr(self, "standby_title", None) is not None:
                self.standby_title.setText("STANDBY — BUFFER READY")

        if normalized == self.normalized_audio_path(self.transport.current_file or ""):
            display_title = self.playlist_display_title_for_path(normalized)
            if not self.transport.running:
                self.set_status(f"READY — {display_title}")

        self.refresh_playlist_preload_tooltips()

    def on_transport_buffer_state(
        self,
        path: str,
        state: str,
        ahead_seconds: float,
    ) -> None:
        normalized = self.normalized_audio_path(path)
        standby_normalized = self.normalized_audio_path(self.standby_path or "")
        if (
            normalized == standby_normalized
            and getattr(self, "standby_title", None) is not None
        ):
            state_upper = str(state).upper()
            if state_upper == "ERROR":
                self.standby_title.setText("STANDBY — BUFFER ERROR")
            elif state_upper == "READY":
                self.standby_title.setText(f"STANDBY — BUFFER READY {ahead_seconds:.0f}s")
            elif state_upper == "BUFFERING":
                self.standby_title.setText(f"STANDBY — BUFFERING {ahead_seconds:.0f}s")

    def _resolved_media_path(self, path: str) -> str:
        # Path.resolve() touches the filesystem. This lookup runs for every
        # playlist row on every playback refresh, so the resolved form of a
        # path is kept once instead of being re-resolved 60 times a second.
        cache = getattr(self, "_resolved_path_cache", None)
        if cache is None:
            cache = {}
            self._resolved_path_cache = cache

        cached = cache.get(path)
        if cached is not None:
            return cached

        try:
            resolved = str(Path(path).resolve())
        except Exception:
            resolved = path

        if len(cache) > 512:
            cache.clear()
        cache[path] = resolved
        return resolved

    def playlist_item_for_path(
        self,
        path: str,
    ) -> QTableWidgetItem | None:
        normalized = self._resolved_media_path(path)
        for row in range(self.playlist.count()):
            item = self.playlist.item(row)
            if item is None:
                continue
            item_path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not item_path:
                continue
            if self._resolved_media_path(item_path) == normalized:
                return item
        return None

    def update_playlist_item_load_state(
        self,
        path: str,
        state: str,
        percent: int | None = None,
    ) -> None:
        item = self.playlist_item_for_path(path)
        if item is None:
            return

        base_title = str(item.data(PLAYLIST_TITLE_ROLE) or Path(path).name)
        item.setData(
            PLAYLIST_TITLE_ROLE,
            base_title,
        )

        state_upper = state.upper()
        if percent is not None:
            percent = max(0, min(100, int(percent)))
            item.setData(
                PLAYLIST_PROGRESS_ROLE,
                percent,
            )
            item.setText(f"{base_title} — {percent}%")
        elif state_upper == "READY":
            item.setData(
                PLAYLIST_PROGRESS_ROLE,
                100,
            )
            item.setText(f"{base_title} — READY")
        elif state_upper == "ERROR":
            item.setText(f"{base_title} — ERROR")
        elif state_upper == "QUEUED":
            item.setData(
                PLAYLIST_PROGRESS_ROLE,
                0,
            )
            item.setText(f"{base_title} — 0%")

        self.update_playlist_overall_progress()

    def update_playlist_overall_progress(self) -> None:
        total = self.playlist.count()
        if total <= 0:
            loaded = 0
            percent = 0
        else:
            loaded = 0
            progress_sum = 0
            for row in range(total):
                item = self.playlist.item(row)
                if item is None:
                    continue
                progress = int(item.data(PLAYLIST_PROGRESS_ROLE) or 0)
                progress_sum += progress
                if progress >= 100:
                    loaded += 1
            percent = int(progress_sum / total)

        label = getattr(
            self,
            "playlist_load_progress_label",
            None,
        )
        if label is not None:
            label.setText(f"WAVEFORM — {loaded}/{total} — {percent}%")

    def _on_playlist_preload_progress(
        self,
        path: str,
        percent: int,
    ) -> None:
        with self._playlist_preload_lock:
            if path in self._playlist_preload_pending:
                self._playlist_preload_state[path] = f"LOADING {percent}%"

        self.update_playlist_item_load_state(
            path,
            "LOADING",
            percent,
        )
        self.refresh_playlist_preload_tooltips()

    def _on_playlist_preload_ready(
        self,
        path: str,
        preview: object,
        success: bool,
        message: str,
    ) -> None:
        with self._playlist_preload_lock:
            self._playlist_preload_pending.discard(path)
            self._playlist_preload_state[path] = "READY" if success else "ERROR"

        if success:
            payload = preview if isinstance(preview, dict) else {"waveform": preview}
            array = np.asarray(payload.get("waveform"), dtype=np.float32)
            self.standby_preview_cache[path] = array

            if path == self.main_track_path():
                self.main_waveform.set_track(Path(path).name, array)
                self.refresh_waveform_markers()

            if path == self.standby_path:
                self.standby_waveform.set_track(Path(path).name, array)
                self.load_standby_markers(path)
                self.standby_panel.setVisible(True)
        elif message:
            self.set_status(f"WAVEFORM ERROR — {Path(path).name}: {message}")

        self.update_playlist_item_load_state(
            path,
            "READY" if success else "ERROR",
        )
        self.refresh_playlist_preload_tooltips()

    def refresh_playlist_preload_tooltips(self) -> None:
        for row in range(self.playlist.count()):
            item = self.playlist.item(row)
            if item is None:
                continue

            path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not path:
                continue

            state = self._playlist_preload_state.get(
                path,
                "READY" if path in self.standby_preview_cache else "WAITING",
            )
            item.setToolTip(
                f"{path}\nWAVEFORM: {state}\nSTREAM BUFFER RAM: {self.playlist_ram_summary()}"
            )

    def prioritize_playlist_preload(
        self,
        row: int,
    ) -> None:
        priority: list[str] = []
        for candidate_row in (
            row,
            row + 1,
        ):
            if candidate_row < 0 or candidate_row >= self.playlist.count():
                continue
            item = self.playlist.item(candidate_row)
            if item is None:
                continue
            path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if path:
                priority.append(path)

        self.queue_playlist_preload(
            prioritize=priority,
        )

    def playlist_display_title_for_item(
        self,
        item: QTableWidgetItem | None,
    ) -> str:
        if item is None:
            return ""

        path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        title = str(item.data(PLAYLIST_TITLE_ROLE) or "").strip()

        if title:
            return title
        if path:
            portable = getattr(self, "_portable_filename", None)
            if callable(portable):
                return portable(path)
            return Path(path).name
        return ""

    def playlist_display_title_for_path(
        self,
        path: str,
    ) -> str:
        item = self.playlist_item_for_path(path)
        if item is not None:
            return self.playlist_display_title_for_item(item)
        return Path(path).name if path else ""

    def refresh_playlist_item_caption(
        self,
        item: QTableWidgetItem,
    ) -> None:
        title = self.playlist_display_title_for_item(item)
        if bool(item.data(PLAYLIST_MISSING_ROLE)):
            item.setText(f"⚠ {title} — MISSING")
            item.setForeground(QColor("#ff9f43"))
            path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            item.setToolTip(
                f"MISSING MEDIA\nDisplay name: {title}\nOriginal path: {path}\n"
                "Double-click the track or use Relink Media to locate it."
            )
            return
        item.setForeground(QColor("#f0f0f0"))
        current_text = str(item.text() or "")
        suffix = ""

        separator = " — "
        if separator in current_text:
            _old_title, suffix = current_text.split(
                separator,
                1,
            )

        if suffix:
            item.setText(f"{title}{separator}{suffix}")
        else:
            item.setText(title)

        path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        original_name = Path(path).name if path else ""
        tooltip_lines = [
            f"Display name: {title}",
        ]
        if original_name:
            tooltip_lines.append(f"Original file: {original_name}")
        if path:
            tooltip_lines.append(path)

        existing_tooltip = str(item.toolTip() or "")
        if "\nWAVEFORM:" in existing_tooltip:
            tooltip_lines.append(
                "WAVEFORM:"
                + existing_tooltip.split(
                    "\nWAVEFORM:",
                    1,
                )[1]
            )

        item.setToolTip("\n".join(tooltip_lines))

    def rename_playlist_item(
        self,
        item: QTableWidgetItem,
    ) -> None:
        if item is None:
            return

        current_title = self.playlist_display_title_for_item(item)
        new_title, accepted = QInputDialog.getText(
            self,
            "Rename Playlist Track",
            ("Display name\n(the original audio filename is not changed):"),
            QLineEdit.EchoMode.Normal,
            current_title,
        )
        if not accepted:
            return

        new_title = str(new_title).strip()
        path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()

        if not new_title:
            new_title = Path(path).name if path else current_title

        item.setData(
            PLAYLIST_TITLE_ROLE,
            new_title,
        )
        self.refresh_playlist_item_caption(item)

        if path == self.main_track_path():
            self.now_playing.setText(f"NOW: {new_title}")
            self.transport_title_label.setText(new_title.upper())

        if path == str(
            getattr(
                self,
                "standby_path",
                "",
            )
            or ""
        ):
            self.set_status(f"STANDBY — {new_title}")

    def rename_current_playlist_item(
        self,
    ) -> None:
        item = self.playlist.currentItem()
        if item is not None:
            self.rename_playlist_item(item)

    def add_paths(self, paths: list[str]) -> None:
        existing = {
            self.playlist.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.playlist.count())
        }
        added_paths: list[str] = []

        for path in paths:
            full = str(Path(path).resolve())
            if full in existing:
                continue
            if Path(full).suffix.lower() not in AUDIO_EXTENSIONS:
                continue

            base_title = Path(full).name
            item = QTableWidgetItem(f"{base_title} — 0%")
            item.setToolTip(
                f"Display name: {base_title}\n"
                f"Original file: {Path(full).name}\n"
                f"{full}\n"
                "WAVEFORM: QUEUED"
            )
            item.setData(
                Qt.ItemDataRole.UserRole,
                full,
            )
            item.setData(
                PLAYLIST_TITLE_ROLE,
                base_title,
            )
            item.setData(
                PLAYLIST_PROGRESS_ROLE,
                0,
            )
            item.setData(PLAYLIST_MISSING_ROLE, False)
            try:
                metadata = self._safe_media_metadata(full)
            except Exception:
                metadata = {"filename": Path(full).name, "size": 0, "duration": 0.0}
            item.setData(PLAYLIST_MEDIA_META_ROLE, metadata)
            try:
                playlist_fps = int(self.fps.currentText())
            except Exception:
                playlist_fps = 25
            self.playlist.addItem(
                item,
                frames_to_timecode(
                    self.track_offset_frames(full),
                    playlist_fps,
                ),
            )
            existing.add(full)
            added_paths.append(full)

        if self.playlist.currentRow() < 0 and self.playlist.count():
            self.playlist.setCurrentRow(0)
            self.select_playlist_item(self.playlist.item(0))

        priority: list[str] = []
        current_row = self.playlist.currentRow()
        for row in (
            current_row,
            current_row + 1,
        ):
            if 0 <= row < self.playlist.count():
                item = self.playlist.item(row)
                if item is not None:
                    path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
                    if path:
                        priority.append(path)

        self.queue_playlist_preload(
            added_paths,
            prioritize=priority,
        )
        self.update_playlist_overall_progress()

    def add_dropped_playlist_paths(
        self,
        paths: list[str],
    ) -> None:
        audio_paths: list[str] = []

        for raw_path in paths:
            path = Path(raw_path).expanduser()

            if path.is_dir():
                audio_paths.extend(
                    str(candidate)
                    for candidate in sorted(path.rglob("*"))
                    if (candidate.is_file() and candidate.suffix.lower() in AUDIO_EXTENSIONS)
                )
                continue

            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                audio_paths.append(str(path))

        before = self.playlist.count()
        self.add_paths(audio_paths)
        added = self.playlist.count() - before

        if added > 0:
            self.set_status(f"PLAYLIST DROP — {added} AUDIO FILE(S) ADDED")
        else:
            self.set_status("PLAYLIST DROP — NO NEW SUPPORTED AUDIO FILES")

    @staticmethod
    def normalized_csv_header(
        value: object,
    ) -> str:
        return "".join(
            character
            for character in str(value or "").strip().casefold()
            if character.isalnum()
        )

    @staticmethod
    def decode_csv_bytes(
        payload: bytes,
    ) -> str:
        last_error: Exception | None = None

        for encoding in (
            "utf-8-sig",
            "utf-8",
            "cp1258",
            "cp1252",
        ):
            try:
                return payload.decode(encoding)
            except UnicodeDecodeError as exc:
                last_error = exc

        if last_error is not None:
            raise last_error

        return payload.decode(
            "utf-8",
            errors="replace",
        )

    def csv_position_to_media_seconds(
        self,
        value: object,
        *,
        fps: int,
        track_path: str,
    ) -> float:
        text = str(value or "").strip()
        if not text:
            raise ValueError("Position is empty.")

        offset_seconds = self.track_offset_seconds(track_path)
        parts = text.replace(
            ";",
            ":",
        ).split(":")

        if len(parts) == 4:
            absolute_frames = timecode_to_frames(
                text.replace(";", ":"),
                fps,
            )
            absolute_seconds = absolute_frames / fps
        elif len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2].replace(",", "."))
            absolute_seconds = hours * 3600 + minutes * 60 + seconds
        elif len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1].replace(",", "."))
            absolute_seconds = minutes * 60 + seconds
        else:
            absolute_seconds = float(text.replace(",", "."))

        return max(
            0.0,
            absolute_seconds - offset_seconds,
        )

    def read_cue_csv_rows(
        self,
        csv_path: str,
        track_path: str,
    ) -> list[dict[str, object]]:
        text = self.decode_csv_bytes(Path(csv_path).read_bytes())

        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(
                sample,
                delimiters=",;\t",
            )
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(
            io.StringIO(text),
            dialect=dialect,
        )
        if not reader.fieldnames:
            raise ValueError("CSV không có hàng tiêu đề.")

        header_map = {
            self.normalized_csv_header(name): name
            for name in reader.fieldnames
            if name is not None
        }

        def header(
            *aliases: str,
        ) -> str | None:
            for alias in aliases:
                found = header_map.get(self.normalized_csv_header(alias))
                if found is not None:
                    return found
            return None

        track_header = header(
            "Track",
            "Music",
            "Audio",
        )
        type_header = header(
            "Type",
            "Cue Type",
        )
        position_header = header(
            "Position",
            "Time",
            "Timecode",
        )
        cue_no_header = header(
            "Cue No",
            "CueNo",
            "Cue Number",
            "Cue",
        )
        label_header = header(
            "Label",
            "Note",
            "Description",
        )
        fade_header = header(
            "Fade",
        )

        if position_header is None:
            raise ValueError(
                "CSV thiếu cột Position. "
                "Cấu trúc hỗ trợ: "
                "Track, Type, Position, "
                "Cue No, Label, Fade."
            )

        raw_rows = [
            dict(row)
            for row in reader
            if any(str(value or "").strip() for value in row.values())
        ]

        current_name = Path(track_path).name.casefold()
        current_stem = Path(track_path).stem.casefold()

        if track_header is not None:
            matching_rows = []
            for row in raw_rows:
                row_track = str(
                    row.get(
                        track_header,
                        "",
                    )
                    or ""
                ).strip()
                row_name = Path(row_track).name.casefold()
                row_stem = Path(row_track).stem.casefold()

                if not row_track or row_name == current_name or row_stem == current_stem:
                    matching_rows.append(row)

            if matching_rows:
                raw_rows = matching_rows

        fps = int(self.fps.currentText())
        loaded: list[dict[str, object]] = []
        row_errors: list[str] = []

        for csv_row_number, row in enumerate(
            raw_rows,
            start=2,
        ):
            try:
                media_seconds = self.csv_position_to_media_seconds(
                    row.get(
                        position_header,
                        "",
                    ),
                    fps=fps,
                    track_path=track_path,
                )

                cue_type = (
                    str(
                        row.get(
                            type_header,
                            "Choreo",
                        )
                        if type_header is not None
                        else "Choreo"
                    ).strip()
                    or "Choreo"
                )

                cue_no = str(
                    row.get(
                        cue_no_header,
                        "",
                    )
                    if cue_no_header is not None
                    else ""
                ).strip()
                if not cue_no:
                    cue_no = str(len(loaded) + 1)

                label = str(
                    row.get(
                        label_header,
                        "",
                    )
                    if label_header is not None
                    else ""
                ).strip()

                fade = str(
                    row.get(
                        fade_header,
                        "",
                    )
                    if fade_header is not None
                    else ""
                ).strip()

                loaded.append(
                    {
                        "seconds": media_seconds,
                        "name": cue_no,
                        "cue_no": cue_no,
                        "label": label,
                        "type": cue_type,
                        "color": (self.cue_type_color(cue_type)),
                        "fade": fade,
                    }
                )
            except Exception as exc:
                row_errors.append(f"row {csv_row_number}: {exc}")

        if not loaded:
            detail = "; ".join(row_errors[:5]) if row_errors else "No cue rows found."
            raise ValueError(f"Không nhập được cue: {detail}")

        if row_errors:
            self.set_status(f"CSV IMPORT WARNING — {len(row_errors)} ROW(S) SKIPPED")

        loaded.sort(
            key=lambda cue: float(
                cue.get(
                    "seconds",
                    0.0,
                )
            )
        )
        return loaded

    def export_cue_csv(self) -> None:
        track_path = self.main_track_path()
        if not track_path:
            self.show_error("Hãy chọn một bài nhạc trước khi Export Cue.")
            return

        default_name = f"{Path(track_path).stem}_cues.csv"
        export_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Cue",
            default_name,
            "CSV Files (*.csv);;All Files (*.*)",
        )
        if not export_path:
            return

        if not export_path.lower().endswith(".csv"):
            export_path += ".csv"

        cues = sorted(
            self.cues_by_track.get(track_path, []),
            key=lambda cue: float(cue.get("seconds", 0.0)),
        )
        fps = int(self.fps.currentText())
        offset_seconds = self.track_offset_seconds(track_path)

        try:
            target = Path(export_path)
            target.parent.mkdir(parents=True, exist_ok=True)

            with target.open(
                "w",
                encoding="utf-8-sig",
                newline="",
            ) as handle:
                writer = csv.writer(
                    handle,
                    quoting=csv.QUOTE_ALL,
                    lineterminator="\r\n",
                )
                writer.writerow(
                    [
                        "Track",
                        "Type",
                        "Position",
                        "Cue No",
                        "Label",
                        "Fade",
                    ]
                )

                track_name = Path(track_path).name

                for index, cue in enumerate(cues, start=1):
                    cue_seconds = float(cue.get("seconds", 0.0))
                    absolute_seconds = cue_seconds + offset_seconds
                    position = frames_to_timecode(
                        round(absolute_seconds * fps),
                        fps,
                    )

                    cue_no = cue.get(
                        "cue_no",
                        cue.get(
                            "cueNo",
                            cue.get("name", index),
                        ),
                    )
                    cue_type = str(cue.get("type", "Choreo") or "")
                    label = str(cue.get("label", "") or "")
                    fade = cue.get("fade", "")

                    writer.writerow(
                        [
                            track_name,
                            cue_type,
                            position,
                            cue_no,
                            label,
                            fade,
                        ]
                    )

            self.set_status(f"CUE EXPORTED — {len(cues)} CUE(S) — {target.name}")
        except Exception as exc:
            self.show_error(f"Export Cue: {exc}")

    def import_cue_csv(self) -> None:
        # The same gate every cue mutation in the cue controller passes: an
        # import rewrites the whole cue list, which is the largest cue edit
        # there is, and it was the one path a locked viewer could still take.
        if not self.require_cue_edit_rights():
            return
        track_path = self.main_track_path()
        if not track_path:
            self.show_error("Hãy chọn một bài nhạc trước khi Import CSV.")
            return

        csv_path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Cue CSV",
            "",
            "CSV Files (*.csv);;All Files (*.*)",
        )
        if not csv_path:
            return

        try:
            imported = self.read_cue_csv_rows(
                csv_path,
                track_path,
            )
        except Exception as exc:
            self.show_error(f"CSV Import: {exc}")
            return

        current = list(
            self.cues_by_track.get(
                track_path,
                [],
            )
        )
        mode = "replace"

        if current:
            box = QMessageBox(self)
            box.setWindowTitle("Import Cue CSV")
            box.setText(f"Bài nhạc đang có {len(current)} cue.")
            box.setInformativeText(
                "Replace sẽ thay toàn bộ cue hiện tại. Merge sẽ giữ cue cũ và thêm cue từ CSV."
            )

            replace_button = box.addButton(
                "Replace",
                QMessageBox.ButtonRole.AcceptRole,
            )
            merge_button = box.addButton(
                "Merge",
                QMessageBox.ButtonRole.ActionRole,
            )
            cancel_button = box.addButton(
                "Cancel",
                QMessageBox.ButtonRole.RejectRole,
            )
            box.setDefaultButton(replace_button)
            box.exec()

            clicked = box.clickedButton()
            if clicked is cancel_button:
                return
            if clicked is merge_button:
                mode = "merge"

        self.push_cue_undo("IMPORT CSV")

        unknown_types: list[str] = []
        known_types = {name for name, _color in self.cue_type_defs}
        for cue in imported:
            cue_type = str(
                cue.get(
                    "type",
                    "Choreo",
                )
            )
            if cue_type not in known_types:
                known_types.add(cue_type)
                unknown_types.append(cue_type)
                self.cue_type_defs.append(
                    (
                        cue_type,
                        "#ffb020",
                    )
                )
                cue["color"] = "#ffb020"

        if unknown_types:
            self.ensure_cue_type_meta()
            self.rebuild_cue_type_buttons()

        merged = current + imported if mode == "merge" else imported

        merged.sort(
            key=lambda cue: float(
                cue.get(
                    "seconds",
                    0.0,
                )
            )
        )

        for cue_id, cue in enumerate(
            merged,
            start=1,
        ):
            cue["id"] = cue_id

        self.cues_by_track[track_path] = merged
        self._active_playback_cue_id = None
        self._last_playhead_position = -1.0

        self.refresh_cue_table()
        self.save_cue_csv(track_path)
        self.set_status(f"CSV IMPORTED — {len(imported)} CUE(S) — {Path(csv_path).name}")

    def add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add Music Files",
            "",
            (
                "Audio Files (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.oga "
                "*.aiff *.aif *.wma *.opus *.caf);;All Files (*.*)"
            ),
        )
        self.add_paths(paths)

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add Music Folder")
        if not folder:
            return

        paths = [
            str(path)
            for path in sorted(Path(folder).rglob("*"))
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
        ]
        self.add_paths(paths)

    def remove_selected(self) -> None:
        row = self.playlist.currentRow()
        if row < 0:
            self.set_status("NO PLAYLIST TRACK SELECTED")
            return

        item = self.playlist.item(row)
        if item is None:
            self.set_status("NO PLAYLIST TRACK SELECTED")
            return

        title = str(item.text() or "Selected track").strip()

        answer = silent_message_box(
            self,
            "Confirm Remove",
            (
                "Remove this track from the playlist?\n\n" + title + "\n\n"
                "The audio file on disk will not be deleted."
            ),
            buttons=(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
            default_button=QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
            self.set_status("REMOVE CANCELLED")
            return

        removed_item = self.playlist.takeItem(row)
        if removed_item is None:
            return

        removed_path = str(removed_item.data(Qt.ItemDataRole.UserRole) or "")
        self._ram_ready_paths.discard(removed_path)

        if self._pending_play_after_ram_path == removed_path:
            self._pending_play_after_ram_path = ""
        if getattr(self, "_pending_activate_after_ram_path", "") == removed_path:
            self._pending_activate_after_ram_path = ""
            self._pending_activate_after_ram_autoplay = False

        self.set_status("REMOVED FROM PLAYLIST — " + title)

    def current_playlist_path(self) -> str | None:
        item = self.playlist.currentItem()
        if item is None:
            return None
        return str(item.data(Qt.ItemDataRole.UserRole))

    def load_current_track(self) -> bool:
        path = self.current_playlist_path()
        if not path:
            self.show_error("Playlist chưa có bài được chọn.")
            return False

        try:
            self.transport.load_audio(path)
            display_title = self.playlist_display_title_for_path(path)
            self.now_playing.setText(f"NOW: {display_title}")
            self.set_status(
                f"LOADED — {display_title} — {seconds_to_clock(self.transport.duration)}"
            )
            return True
        except Exception as exc:
            self.show_error(str(exc))
            return False

    def select_path(self, path: str) -> None:
        for row in range(self.playlist.count()):
            item = self.playlist.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                self.playlist.setCurrentRow(row)
                self.select_playlist_item(item)
                return

    def _start_main_preview_load(
        self,
        path: str,
    ) -> None:
        self._main_preview_token += 1
        token = self._main_preview_token

        cached = self.standby_preview_cache.get(path)
        if cached is not None:
            self.main_preview_ready.emit(path, cached, token)
            return

        try:
            future = self.media_workers.submit_waveform(path)
        except Exception as exc:
            self.preview_failed.emit(path, str(exc))
            return

        def finished(future) -> None:
            try:
                preview = future.result()
                self.main_preview_ready.emit(path, preview, token)
            except Exception as exc:
                self.preview_failed.emit(path, str(exc))

        future.add_done_callback(finished)

    def _start_standby_preview_load(
        self,
        path: str,
        row: int,
    ) -> None:
        self._standby_preview_token += 1
        token = self._standby_preview_token

        cached = self.standby_preview_cache.get(path)
        if cached is not None:
            self.standby_preview_ready.emit(
                path,
                cached,
                row,
                token,
            )
            return

        try:
            future = self.media_workers.submit_waveform(path)
        except Exception as exc:
            self.preview_failed.emit(path, str(exc))
            return

        def finished(future) -> None:
            try:
                preview = future.result()
                self.standby_preview_ready.emit(path, preview, row, token)
            except Exception as exc:
                self.preview_failed.emit(path, str(exc))

        future.add_done_callback(finished)

    def _apply_main_preview(
        self,
        path: str,
        preview: object,
        token: int,
    ) -> None:
        if token != self._main_preview_token:
            return
        if path != self.transport.current_file:
            return

        array = np.asarray(preview)
        self.standby_preview_cache[path] = array
        self.load_cue_csv_if_available(path)

        if path != self.main_track_path():
            return

        self.main_waveform.set_track(
            Path(path).name,
            array,
        )

        if not self._updating_cue_table:
            self.refresh_waveform_markers()

    def _apply_standby_preview(
        self,
        path: str,
        preview: object,
        row: int,
        token: int,
    ) -> None:
        if token != self._standby_preview_token:
            return
        if path != self.standby_path:
            return
        if row != self.standby_playlist_row:
            return

        array = np.asarray(preview)
        self.standby_preview_cache[path] = array
        self.standby_title.setText("STANDBY")
        self.standby_waveform.set_track(
            Path(path).name,
            array,
        )
        self.load_standby_markers(path)

        if path != self.standby_path:
            return

        self.standby_panel.setVisible(True)

    def _on_preview_failed(
        self,
        path: str,
        message: str,
    ) -> None:
        # Playback remains usable even if waveform preview fails.
        self.set_status(f"WAVEFORM ERROR — {Path(path).name}: {message}")

    def load_track_offsets(self) -> None:
        # The active project is now the only persistence source. Legacy sidecars
        # are imported by ProjectControllerMixin when an old project is opened.
        self.track_offsets = {}

    def save_track_offsets(self) -> None:
        # Do not create track_offsets.json. The current in-memory offsets are
        # serialized by build_project_payload() into the .Tracyy file.
        if hasattr(self, "mark_project_dirty"):
            self.mark_project_dirty()

    def track_offset_frames(self, path: str | None) -> int:
        if not path:
            return 0
        return max(0, int(self.track_offsets.get(str(path), 0)))

    def track_offset_seconds(self, path: str | None) -> float:
        fps = int(self.fps.currentText())
        return self.track_offset_frames(path) / max(1, fps)

    def main_offset_frames(self) -> int:
        return self.track_offset_frames(self.main_track_path())

    def main_offset_seconds(self) -> float:
        fps = int(self.fps.currentText())
        return self.main_offset_frames() / max(1, fps)

    def media_to_timecode_seconds(
        self,
        media_seconds: float,
        path: str | None = None,
    ) -> float:
        target_path = path or self.main_track_path()
        return max(
            0.0,
            float(media_seconds) + self.track_offset_seconds(target_path),
        )

    def timecode_to_media_seconds(
        self,
        timecode_seconds: float,
        path: str | None = None,
    ) -> float:
        target_path = path or self.main_track_path()
        return max(
            0.0,
            float(timecode_seconds) - self.track_offset_seconds(target_path),
        )

    def refresh_playlist_offset_cells(self) -> None:
        playlist = getattr(self, "playlist", None)
        if playlist is None or not hasattr(playlist, "set_offset_for_row"):
            return

        try:
            fps = int(self.fps.currentText())
        except Exception:
            fps = 25

        playlist.blockSignals(True)
        try:
            for row in range(playlist.count()):
                if hasattr(playlist, "is_offset_editing") and playlist.is_offset_editing(row):
                    continue
                item = playlist.item(row)
                if item is None:
                    continue
                path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
                playlist.set_offset_for_row(
                    row,
                    frames_to_timecode(
                        self.track_offset_frames(path),
                        fps,
                    ),
                )
        finally:
            playlist.blockSignals(False)

    def update_main_offset_ui(self) -> None:
        """Compatibility hook: Offset now lives only in the Playlist table."""
        self.refresh_playlist_offset_cells()

    def on_playlist_cell_clicked(
        self,
        row: int,
        column: int,
    ) -> None:
        """Handle Playlist clicks without disturbing Offset editing.

        Clicking the Offset column only selects that row/cell. Loading a track
        is reserved for the Playlist column, so an Offset click cannot trigger
        a track load -> UI refresh -> editor rewrite cycle.
        """
        if row < 0 or row >= self.playlist.count():
            return
        if column == self.playlist.OFFSET_COLUMN:
            return

        item = self.playlist.item(
            row,
            self.playlist.PLAYLIST_COLUMN,
        )
        if item is not None and bool(item.data(PLAYLIST_MISSING_ROLE)):
            self.set_status(f"MEDIA MISSING — {self.playlist_display_title_for_item(item)}")
            return
        self.select_playlist_item(item)

    def on_playlist_rows_reordered(
        self,
        source_row: int,
        target_row: int,
    ) -> None:
        """Repair row-index state after a safe table reorder.

        Track identity is path-based; row numbers are derived again after the
        move. This prevents Main/Standby/preload state from pointing at a
        different song after drag/drop.
        """
        main_path = str(self.main_track_path() or "").strip()
        standby_path = str(getattr(self, "standby_path", "") or "").strip()

        if main_path:
            self.playing_playlist_row = self.playlist_row_for_path(main_path)

        if standby_path:
            standby_row = self.playlist_row_for_path(standby_path)
            self.standby_playlist_row = standby_row
            self.standby_row = standby_row
            if getattr(self, "_standby_preloaded_path", "") == standby_path:
                self._standby_preload_row = standby_row

        marker = getattr(self, "mark_project_dirty", None)
        if callable(marker):
            marker()

        self.update_playlist_overall_progress()
        self.set_status(f"PLAYLIST MOVED — {source_row + 1} → {target_row + 1}")

    def refresh_playlist_row_states(self) -> None:
        """Keep the card dots in step with what is playing and what is armed.

        Derived from the paths rather than pushed from every place that starts
        or arms a track: those places are many and a missed one would leave a
        row showing the wrong colour, which on stage reads as the wrong song
        being ready.
        """
        playlist = getattr(self, "playlist", None)
        if playlist is None or not hasattr(playlist, "set_row_states"):
            return

        main_path = str(self.main_track_path() or "").strip()
        standby_path = str(getattr(self, "standby_path", "") or "").strip()

        playing_row = self.playlist_row_for_path(main_path) if main_path else -1
        standby_row = self.playlist_row_for_path(standby_path) if standby_path else -1

        # A track cannot be both. If the same row is armed and playing, the
        # green wins: that is the one the audience is hearing.
        if standby_row == playing_row:
            standby_row = -1

        playlist.set_row_states(
            -1 if playing_row is None else int(playing_row),
            -1 if standby_row is None else int(standby_row),
        )

    def on_playlist_cell_double_clicked(
        self,
        row: int,
        column: int,
    ) -> None:
        if column == self.playlist.OFFSET_COLUMN:
            self.playlist.begin_offset_edit(row)
            return

        # The card shows the title and the offset one above the other in a
        # single cell, so which one was aimed at decides what a double click
        # means: rename the track, or retime it.
        if self.playlist.pressed_zone(row) == "offset":
            self.playlist.begin_offset_edit(row)
            return

        playlist_item = self.playlist.item(
            row,
            self.playlist.PLAYLIST_COLUMN,
        )
        if playlist_item is not None:
            if bool(playlist_item.data(PLAYLIST_MISSING_ROLE)):
                self.relink_single_playlist_row(row)
                return
            self.rename_playlist_item(playlist_item)

    def on_playlist_item_changed(
        self,
        item: QTableWidgetItem | None,
    ) -> None:
        if item is None:
            return
        if item.column() != self.playlist.OFFSET_COLUMN:
            return

        row = item.row()
        source_item = self.playlist.item(
            row,
            self.playlist.PLAYLIST_COLUMN,
        )
        if source_item is None:
            return

        path = str(
            item.data(Qt.ItemDataRole.UserRole)
            or source_item.data(Qt.ItemDataRole.UserRole)
            or ""
        ).strip()
        if not path:
            return

        try:
            fps = int(self.fps.currentText())
        except Exception:
            fps = 25

        previous_frames = self.track_offset_frames(path)
        try:
            frames = timecode_to_frames(
                str(item.text() or "00:00:00:00"),
                fps,
            )
        except Exception:
            self.playlist.blockSignals(True)
            try:
                item.setText(frames_to_timecode(previous_frames, fps))
            finally:
                self.playlist.blockSignals(False)
            self.set_status("OFFSET INVALID — use HH:MM:SS:FF")
            return

        canonical = frames_to_timecode(frames, fps)
        if item.text() != canonical:
            self.playlist.blockSignals(True)
            try:
                item.setText(canonical)
            finally:
                self.playlist.blockSignals(False)

        self.track_offsets[str(path)] = int(frames)
        self.save_track_offsets()

        if path == self.main_track_path():
            with self.transport._lock:
                self.transport.timecode_offset_seconds = frames / max(1, fps)
                self.transport._position_revision += 1
            self.refresh_cue_table()
            self.refresh_position()

        self.set_status(f"OFFSET SET — {Path(path).name} — {canonical}")
        if self.input_timecode_chase_enabled():
            self.update_input_timecode_track_matching(force=True)

    def playlist_row_for_path(self, path: str) -> int:
        for row in range(self.playlist.count()):
            item = self.playlist.item(row)
            if str(item.data(Qt.ItemDataRole.UserRole)) == path:
                return row
        return -1

    def load_standby_markers(self, path: str) -> None:
        if path != self.standby_path:
            return

        self.load_cue_csv_if_available(path)

        try:
            info = sf.info(path)
            duration = info.frames / float(info.samplerate) if info.samplerate > 0 else 0.0
        except Exception:
            duration = 0.0

        if duration <= 0:
            cues = self.cues_by_track.get(path, [])
            duration = max([float(cue.get("seconds", 0.0)) for cue in cues] + [1.0])

        if path != self.standby_path:
            return

        self.standby_waveform.set_markers(self.markers_for_track(path, duration))

    def clear_standby(self, title: str = "") -> None:
        self._cancel_progressive_standby_load()
        self._standby_warmup_generation += 1
        del title
        self._standby_preview_token += 1
        self.standby_playlist_row = -1
        self.standby_path = None
        self.standby_title.clear()
        self.standby_waveform.clear_track()
        self.standby_panel.setVisible(False)
        self.transport.preload_music("")

    def refresh_standby_display(self) -> None:
        path = str(
            getattr(
                self,
                "standby_path",
                "",
            )
            or ""
        ).strip()
        row = int(
            getattr(
                self,
                "standby_row",
                -1,
            )
        )

        if not path:
            return

        title = Path(path).name
        if 0 <= row < self.playlist.count():
            item = self.playlist.item(row)
            if item is not None:
                title = item.text()

        for attribute_name in (
            "standby_label",
            "standby_name_label",
            "standby_track_label",
            "standby_title_label",
            "standby_filename_label",
            "standby_display_label",
        ):
            candidate = getattr(
                self,
                attribute_name,
                None,
            )
            if candidate is None:
                continue
            try:
                candidate.setText(title)
                candidate.setVisible(True)
                candidate.setToolTip(path)
            except Exception:
                pass

    def _apply_ram_ready_standby(
        self,
        row: int,
        path: str,
    ) -> None:
        # The Standby startup buffer is already ready. Reuse the bounded stream
        # and cached waveform immediately.
        timer = getattr(
            self,
            "_standby_preload_timer",
            None,
        )
        if timer is not None:
            timer.stop()

        cancel_method = getattr(
            self,
            "_cancel_progressive_standby_load",
            None,
        )
        if cancel_method is not None:
            try:
                cancel_method()
            except Exception:
                pass

        self._standby_preload_row = row
        self._standby_preload_path = path
        self._standby_preloaded_path = path

        if hasattr(self, "standby_playlist_row"):
            self.standby_playlist_row = row

        self.standby_row = row
        self.standby_path = path

        panel = getattr(
            self,
            "standby_panel",
            None,
        )
        if panel is not None:
            panel.setVisible(True)

        title_widget = getattr(
            self,
            "standby_title",
            None,
        )
        if title_widget is not None:
            title_widget.setText("STANDBY — BUFFER READY")

        waveform = self.standby_preview_cache.get(path)
        if waveform is not None:
            try:
                self.standby_waveform.set_track(
                    Path(path).name,
                    waveform,
                )
                self.load_standby_markers(path)
            except Exception:
                pass
        else:
            # Waveform cache and decoded audio are separate. Generate only the
            # lightweight visual preview when it is missing; do not reload or
            # decode the audio file.
            try:
                self._start_standby_preview_load(
                    path,
                    row,
                )
            except Exception:
                pass

    def set_standby_selection_deferred(
        self,
        row: int,
        path: str,
    ) -> None:
        self._cancel_progressive_standby_load()
        if row < 0 or row >= self.playlist.count() or not path:
            return
        source_item = self.playlist.item(row)
        if (
            source_item is None
            or bool(source_item.data(PLAYLIST_MISSING_ROLE))
            or not Path(path).exists()
        ):
            return

        self.standby_row = row
        self.standby_playlist_row = row
        self.standby_path = path
        self._standby_preload_row = row
        self._standby_preload_path = path
        self.standby_panel.setVisible(True)

        cached = self.standby_preview_cache.get(path)
        if cached is not None:
            self.standby_waveform.set_track(Path(path).name, cached)
            self.load_standby_markers(path)
        else:
            self.standby_waveform.clear_track()
            self.queue_playlist_preload([path], prioritize=[path])

        if self.transport.is_buffer_ready(path):
            self._apply_ram_ready_standby(row, path)
            return

        self.standby_title.setText("STANDBY — BUFFERING")
        # Debounce real decoder creation during rapid playlist browsing.
        self._standby_preload_timer.stop()
        self._standby_preload_timer.start()

    def _perform_standby_preload(self) -> None:
        row = int(getattr(self, "_standby_preload_row", -1))
        path = str(getattr(self, "_standby_preload_path", "") or "")
        if (
            row < 0
            or row >= self.playlist.count()
            or not path
            or path != self.standby_path
            or path == self.main_track_path()
        ):
            return

        try:
            self.transport.preload_music(path)
            if self.transport.is_buffer_ready(path):
                self._standby_preloaded_path = path
                self.standby_title.setText("STANDBY — BUFFER READY")
            else:
                self.standby_title.setText("STANDBY — BUFFERING")
        except Exception as exc:
            self.standby_title.setText("STANDBY — BUFFER ERROR")
            self.set_status(f"STANDBY BUFFER ERROR — {exc}")

    def _start_progressive_standby_load(
        self,
        row: int,
        path: str,
    ) -> None:
        if path and self.is_audio_ready_in_ram(path):
            self._standby_preloaded_path = path
            return

        self._standby_progressive_generation += 1
        generation = self._standby_progressive_generation

        self._standby_progressive_cancel.set()
        self._standby_progressive_cancel = threading.Event()
        cancel_event = self._standby_progressive_cancel

        self._standby_progressive_path = path
        self._standby_progressive_loaded = 0

        try:
            total_size = Path(path).stat().st_size
        except Exception:
            total_size = 0

        self._standby_progressive_total = total_size

        # The first 12 MB is considered "ready to switch". The rest continues
        # loading slowly while Main plays.
        self._standby_ready_threshold = min(
            total_size,
            4 * 1024 * 1024,
        )

        self.standby_panel.setVisible(True)
        self.standby_title.setText("STANDBY — LOADING 0%")
        self.standby_waveform.clear_track()

        # Keep the existing asynchronous waveform loader. It prepares the
        # visual preview while the file is warmed progressively below.
        cached = self.standby_preview_cache.get(path)
        if cached is not None:
            self.standby_waveform.set_track(
                Path(path).name,
                cached,
            )
            self.load_standby_markers(path)
        else:
            self._start_standby_preview_load(
                path,
                row,
            )

        def worker() -> None:
            loaded = 0
            success = False
            try:
                with Path(path).open("rb") as handle:
                    while not cancel_event.is_set():
                        chunk = handle.read(64 * 1024)
                        if not chunk:
                            success = True
                            break

                        loaded += len(chunk)

                        self.standby_progressive_update.emit(
                            generation,
                            path,
                            loaded,
                            total_size,
                            False,
                        )

                        # Keep the entire Standby load slow and even from the
                        # first chunk. This avoids the initial CPU/disk spike
                        # that could disturb Main audio.
                        time.sleep(0.025)

                if not cancel_event.is_set():
                    self.standby_progressive_update.emit(
                        generation,
                        path,
                        loaded,
                        total_size,
                        success,
                    )
            except Exception:
                if not cancel_event.is_set():
                    self.standby_progressive_update.emit(
                        generation,
                        path,
                        loaded,
                        total_size,
                        False,
                    )

        threading.Thread(
            target=worker,
            name="TracyyProgressiveStandby",
            daemon=True,
        ).start()

    def _on_standby_progressive_update(
        self,
        generation: int,
        path: str,
        loaded: int,
        total: int,
        finished: bool,
    ) -> None:
        if generation != self._standby_progressive_generation or path != self.standby_path:
            return

        self._standby_progressive_loaded = loaded
        self._standby_progressive_total = total

        percent = int((loaded / total) * 100) if total > 0 else 0
        percent = max(0, min(100, percent))

        ready = loaded >= self._standby_ready_threshold and self._standby_ready_threshold > 0

        if finished or (total > 0 and loaded >= total):
            self.standby_title.setText("STANDBY — READY 100%")
            self._standby_preloaded_path = path
        elif ready:
            self.standby_title.setText(f"STANDBY — READY {percent}%")
            self._standby_preloaded_path = path
        else:
            self.standby_title.setText(f"STANDBY — LOADING {percent}%")

        # Loading progress is UI metadata only. Do not feed it into the
        # waveform playhead/progress API: doing so can make Standby look partly
        # played while the file is merely warming in the background.

    def _cancel_progressive_standby_load(self) -> None:
        self._standby_progressive_generation += 1
        self._standby_progressive_cancel.set()
        self._standby_progressive_path = ""
        self._standby_progressive_loaded = 0
        self._standby_progressive_total = 0

    def _on_standby_warmup_ready(
        self,
        generation: int,
        path: str,
        success: bool,
    ) -> None:
        if generation != self._standby_warmup_generation or path != self.standby_path:
            return

        if success:
            self._standby_preloaded_path = path

        # Do not decode into the real Standby deck while Main is playing.
        if not bool(
            getattr(
                self.transport,
                "running",
                False,
            )
        ):
            self._prepare_real_standby_if_safe(
                generation,
                path,
            )

    def _prepare_real_standby_if_safe(
        self,
        generation: int,
        path: str,
    ) -> None:
        if (
            generation != self._standby_warmup_generation
            or path != self.standby_path
            or path == self.main_track_path()
        ):
            return

        if bool(
            getattr(
                self.transport,
                "running",
                False,
            )
        ):
            return

        try:
            self.transport.preload_music(path)
            self._standby_preloaded_path = path
        except Exception as exc:
            self.set_status(f"STANDBY PRELOAD ERROR — {exc}")

    def load_standby_preview(self, row: int) -> None:
        candidate_row = max(0, int(row))
        while candidate_row < self.playlist.count():
            item = self.playlist.item(candidate_row)
            if item is None:
                candidate_row += 1
                continue
            path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            unavailable = (
                not path
                or bool(item.data(PLAYLIST_MISSING_ROLE))
                or not Path(path).exists()
                or path == self.transport.current_file
            )
            if unavailable:
                candidate_row += 1
                continue
            self.set_standby_selection_deferred(candidate_row, path)
            return

        self.clear_standby()

    def prepare_next_standby(self) -> None:
        row = self.playing_playlist_row
        if row < 0:
            row = self.playlist_row_for_path(self.transport.current_file)
        self.load_standby_preview(row + 1)

    def transport_is_live(self) -> bool:
        """True only while Main is genuinely still playing.

        Selection routing depends on this: a click during playback becomes a
        Standby selection, a click while stopped loads Main. ``running`` alone
        is not a safe answer — a decoder that fails to report EOF leaves it
        stuck True on a track that has already finished, and from then on every
        click lands in Standby with no way to put anything back on Main.
        """
        if not bool(getattr(self.transport, "running", False)):
            return False

        duration = float(getattr(self.transport, "duration", 0.0) or 0.0)
        if duration <= 0.0:
            return True

        position = float(getattr(self.transport, "position_seconds", 0.0) or 0.0)
        return position < duration - 0.05

    def activate_playlist_row(
        self,
        row: int,
        autoplay: bool,
    ) -> bool:
        self.prioritize_playlist_preload(row)
        if hasattr(self, "_standby_preload_timer"):
            self._standby_preload_timer.stop()

        # Safety rule shared by Master and Follow:
        # while Main is playing, a normal non-autoplay selection is always a
        # Standby selection. Only explicit remote SELECT_MAIN, Next/Previous,
        # promotion, or other autoplay actions may replace Main.
        _normal_selection_while_playing = self.transport_is_live() and not autoplay

        if _normal_selection_while_playing:
            item = self.playlist.item(row)
            if item is None:
                return False

            selected_path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not selected_path:
                return False

            current_main = self.main_track_path()
            if selected_path == current_main:
                return False

            self.set_standby_selection_deferred(
                row,
                selected_path,
            )
            return True

        if row < 0 or row >= self.playlist.count():
            return False

        item = self.playlist.item(row)
        if item is None or bool(item.data(PLAYLIST_MISSING_ROLE)):
            if item is not None:
                self.set_status(f"MEDIA MISSING — {self.playlist_display_title_for_item(item)}")
            return False
        path = str(item.data(Qt.ItemDataRole.UserRole))
        if not path or not Path(path).exists():
            item.setData(PLAYLIST_MISSING_ROLE, True)
            self.refresh_playlist_item_caption(item)
            return False

        try:
            self._cue_table_load_token += 1
            self._cue_table_pending_cues = []
            self._cue_table_pending_index = 0
            self._cue_table_pending_path = path

            self.transport.pause()
            self.transport.load_audio(path)
            with self.transport._lock:
                self.transport.timecode_offset_seconds = self.track_offset_seconds(path)
                self.transport._position_revision += 1

            self.playing_playlist_row = row
            self.playlist.blockSignals(True)
            self.playlist.setCurrentRow(row)
            self.playlist.blockSignals(False)

            self.load_cue_csv_if_available(path)
            self._active_playback_cue_id = None
            self._last_playhead_position = -1.0
            self.refresh_cue_table()

            self.main_waveform.clear_track()
            display_title = self.playlist_display_title_for_path(path)
            self.now_playing.setText(f"NOW: {display_title}")
            if self.transport.is_buffer_ready(path):
                self.set_status(f"READY — {display_title}")
            else:
                self.set_status(f"BUFFERING — {display_title}")
            self.update_main_offset_ui()
            self.sync_cue_time_editor_from_selection()

            # Main audio is already assigned before waveform generation starts.
            self._start_main_preview_load(path)
            self.prepare_next_standby()

            if autoplay:
                self.play()
            return True
        except Exception as exc:
            self.show_error(str(exc))
            return False

    def promote_standby(
        self,
        autoplay: bool,
    ) -> bool:
        standby_path = str(
            getattr(
                self,
                "standby_path",
                "",
            )
            or ""
        )
        standby_path = str(
            getattr(
                self,
                "standby_path",
                "",
            )
            or ""
        )
        if standby_path:
            try:
                if standby_path != self._standby_preloaded_path:
                    self.transport.preload_music(standby_path)
                    self._standby_preloaded_path = standby_path
            except Exception:
                pass

        if self.standby_playlist_row < 0 or not self.standby_path:
            return False

        path = self.standby_path
        row = self.standby_playlist_row

        promoted = self.transport.promote_preloaded_music()
        with self.transport._lock:
            self.transport.timecode_offset_seconds = self.track_offset_seconds(path)
            self.transport._position_revision += 1
        if promoted != path:
            return self.activate_playlist_row(
                row,
                autoplay,
            )

        self.playing_playlist_row = row
        self.playlist.blockSignals(True)
        self.playlist.setCurrentRow(row)
        self.playlist.blockSignals(False)

        self.load_cue_csv_if_available(path)
        self._active_playback_cue_id = None
        self._last_playhead_position = -1.0
        self.refresh_cue_table()
        display_title = self.playlist_display_title_for_path(path)
        self.now_playing.setText(f"NOW: {display_title}")

        cached = self.standby_preview_cache.get(path)
        if cached is not None:
            self.main_waveform.set_track(
                Path(path).name,
                cached,
            )
            self.refresh_waveform_markers()
        else:
            self.main_waveform.clear_track()
            self._start_main_preview_load(path)

        self.prepare_next_standby()

        self.update_main_offset_ui()
        self.sync_cue_time_editor_from_selection()

        if autoplay:
            self.play()
        return True

    def select_playlist_item(
        self,
        item: QTableWidgetItem | None,
    ) -> None:
        # QTableWidget can emit selection/cell signals while a row is
        # being inserted or rebuilt.  In that short window item(row, 0)
        # may legitimately be None; ignore the transient signal instead
        # of dereferencing it.
        if item is None:
            return
        if bool(item.data(PLAYLIST_MISSING_ROLE)):
            self.set_status(f"MEDIA MISSING — {self.playlist_display_title_for_item(item)}")
            return

        row = self.playlist.row(item)
        if row < 0:
            return
        path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()

        if not path:
            return

        if self.input_timecode_chase_enabled():
            if not self.input_timecode_has_live_chase():
                self.activate_input_timecode_preview_track(path)
                return

            if path not in self._input_timecode_match_paths:
                self.set_status("TRACK DOES NOT MATCH CURRENT INPUT TIMECODE")
                self.refresh_input_timecode_playlist_highlights()
                return

            self.activate_input_timecode_track(
                path,
                operator_selected=True,
            )
            return

        if self.transport_is_live():
            if path == self.transport.current_file:
                return

            self.set_standby_selection_deferred(
                row,
                path,
            )
            self.set_status(f"STANDBY — {self.playlist_display_title_for_path(path)}")
            return

        self.activate_playlist_row(
            row,
            autoplay=False,
        )
