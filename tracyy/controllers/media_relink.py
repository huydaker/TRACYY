from __future__ import annotations

import os
from pathlib import Path

import soundfile as sf
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
)

from tracyy.widgets import (
    AUDIO_EXTENSIONS,
    PLAYLIST_MEDIA_META_ROLE,
    PLAYLIST_MISSING_ROLE,
    PLAYLIST_PROGRESS_ROLE,
    PLAYLIST_TITLE_ROLE,
    frames_to_timecode,
)


class MediaRelinkControllerMixin:
    """Project-media relocation support.

    A missing audio file remains in the Playlist and keeps its Cue/Offset state.
    Relinking only changes the media path; it never deletes show programming.
    """

    @staticmethod
    def _portable_filename(path: str) -> str:
        # A .Tracyy project may move between Windows and macOS, so do not let
        # the host OS decide whether backslashes/slashes are separators.
        normalized = str(path or "").replace("\\", "/")
        return normalized.rsplit("/", 1)[-1]

    @staticmethod
    def _safe_media_metadata(path: str | Path) -> dict[str, object]:
        candidate = Path(path).expanduser()
        result: dict[str, object] = {
            "filename": MediaRelinkControllerMixin._portable_filename(str(candidate)),
            "size": 0,
            "duration": 0.0,
        }
        try:
            if candidate.exists() and candidate.is_file():
                result["size"] = int(candidate.stat().st_size)
        except Exception:
            pass
        try:
            info = sf.info(str(candidate))
            result["duration"] = float(info.frames) / max(1.0, float(info.samplerate))
        except Exception:
            pass
        return result

    @staticmethod
    def _relative_media_path(media_path: str, project_path: str | Path | None) -> str:
        if not media_path or not project_path:
            return ""
        try:
            base = Path(project_path).expanduser().resolve().parent
            target = Path(media_path).expanduser().resolve()
            relative = os.path.relpath(str(target), str(base))
            return str(relative).replace("\\", "/")
        except Exception:
            return ""

    def project_media_entry_for_item(
        self,
        item: QTableWidgetItem,
        project_path: str | Path | None,
    ) -> dict[str, object]:
        path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        title = self.playlist_display_title_for_item(item)
        stored_meta = item.data(PLAYLIST_MEDIA_META_ROLE)
        meta = dict(stored_meta) if isinstance(stored_meta, dict) else {}

        live_meta = self._safe_media_metadata(path)
        if int(live_meta.get("size", 0) or 0) > 0:
            meta.update(live_meta)
        else:
            meta.setdefault("filename", self._portable_filename(path) if path else title)
            meta.setdefault("size", 0)
            meta.setdefault("duration", 0.0)

        relative_path = self._relative_media_path(path, project_path)
        if relative_path:
            meta["relative_path"] = relative_path
        elif "relative_path" not in meta:
            meta["relative_path"] = ""

        return {
            "path": path,
            "track_id": self.track_id_for_path(path),
            "display_name": title,
            "relative_path": str(meta.get("relative_path", "") or ""),
            "filename": str(meta.get("filename", "") or self._portable_filename(path)),
            "size": int(meta.get("size", 0) or 0),
            "duration": float(meta.get("duration", 0.0) or 0.0),
        }

    @staticmethod
    def _candidate_matches_metadata(candidate: Path, metadata: dict[str, object]) -> int:
        score = 0
        expected_name = str(metadata.get("filename", "") or "").strip().lower()
        if expected_name and candidate.name.lower() == expected_name:
            score += 100
        elif expected_name:
            return -1

        expected_size = int(metadata.get("size", 0) or 0)
        if expected_size > 0:
            try:
                if int(candidate.stat().st_size) == expected_size:
                    score += 80
                else:
                    score -= 25
            except Exception:
                score -= 25
        return score

    def resolve_project_media_entry(
        self,
        project_path: str | Path,
        entry: dict[str, object],
    ) -> tuple[str, bool, dict[str, object]]:
        """Resolve absolute/relative media without discarding missing entries."""
        project_file = Path(project_path).expanduser().resolve()
        project_dir = project_file.parent
        original_path = str(entry.get("path", "") or "").strip()
        filename = str(entry.get("filename", "") or "").strip()
        if not filename and original_path:
            filename = self._portable_filename(original_path)

        metadata = {
            "filename": filename,
            "size": int(entry.get("size", 0) or 0),
            "duration": float(entry.get("duration", 0.0) or 0.0),
            "relative_path": str(entry.get("relative_path", "") or ""),
            "original_path": original_path,
        }

        candidates: list[Path] = []
        if original_path:
            candidates.append(Path(original_path).expanduser())
        relative_path = str(metadata.get("relative_path", "") or "").strip()
        if relative_path:
            portable_relative = relative_path.replace("\\", "/")
            candidates.append(
                project_dir.joinpath(
                    *[part for part in portable_relative.split("/") if part not in {"", "."}]
                )
            )
        if filename:
            candidates.extend(
                [
                    project_dir / filename,
                    project_dir / "Music" / filename,
                    project_dir / "Media" / filename,
                    project_dir / "Audio" / filename,
                ]
            )

        seen: set[str] = set()
        for candidate in candidates:
            try:
                normalized = str(candidate.resolve())
            except Exception:
                normalized = str(candidate)
            if normalized in seen:
                continue
            seen.add(normalized)
            if (
                candidate.exists()
                and candidate.is_file()
                and candidate.suffix.lower() in AUDIO_EXTENSIONS
            ):
                metadata.update(self._safe_media_metadata(candidate))
                metadata["relative_path"] = self._relative_media_path(
                    str(candidate), project_file
                )
                return str(candidate.resolve()), False, metadata

        # Portable project folder fallback: search only for the original filename.
        if filename:
            matches: list[Path] = []
            try:
                wanted_name = filename.lower()
                for candidate in project_dir.rglob("*"):
                    if (
                        candidate.is_file()
                        and candidate.name.lower() == wanted_name
                        and candidate.suffix.lower() in AUDIO_EXTENSIONS
                    ):
                        matches.append(candidate)
                        if len(matches) >= 64:
                            break
            except Exception:
                matches = []

            if matches:
                scored = sorted(
                    ((self._candidate_matches_metadata(c, metadata), c) for c in matches),
                    key=lambda pair: pair[0],
                    reverse=True,
                )
                if len(scored) == 1 or scored[0][0] > scored[1][0]:
                    candidate = scored[0][1]
                    metadata.update(self._safe_media_metadata(candidate))
                    metadata["relative_path"] = self._relative_media_path(
                        str(candidate), project_file
                    )
                    return str(candidate.resolve()), False, metadata

        missing_path = original_path or filename
        return missing_path, True, metadata

    def add_project_media_item(
        self,
        path: str,
        display_name: str,
        *,
        missing: bool,
        metadata: dict[str, object] | None = None,
    ) -> QTableWidgetItem:
        title = (
            str(display_name or "").strip() or self._portable_filename(path) or "Missing Media"
        )
        item = QTableWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        item.setData(PLAYLIST_TITLE_ROLE, title)
        item.setData(PLAYLIST_PROGRESS_ROLE, 0)
        item.setData(PLAYLIST_MISSING_ROLE, bool(missing))
        item.setData(PLAYLIST_MEDIA_META_ROLE, dict(metadata or {}))

        try:
            fps = int(self.fps.currentText())
        except Exception:
            fps = 25
        self.playlist.addItem(
            item,
            frames_to_timecode(self.track_offset_frames(path), fps),
        )
        self.refresh_playlist_item_caption(item)
        return item

    def playlist_item_is_missing(self, item: QTableWidgetItem | None) -> bool:
        if item is None:
            return False
        if bool(item.data(PLAYLIST_MISSING_ROLE)):
            return True
        path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        return bool(path) and not Path(path).exists()

    def missing_playlist_rows(self) -> list[int]:
        rows: list[int] = []
        for row in range(self.playlist.count()):
            if self.playlist_item_is_missing(self.playlist.item(row)):
                rows.append(row)
        return rows

    def _migrate_track_path_state(self, old_path: str, new_path: str) -> None:
        if not old_path or not new_path or old_path == new_path:
            return

        if old_path in self.track_offsets:
            old_offset = self.track_offsets.pop(old_path)
            self.track_offsets.setdefault(new_path, old_offset)

        # The id follows the file. Losing it here would mint a second id for
        # the same song and split its cues in two on the next save.
        moved_id = self.track_ids.pop(old_path, "")
        if moved_id:
            self.track_ids.setdefault(new_path, moved_id)

        if old_path in self.cues_by_track:
            old_cues = self.cues_by_track.pop(old_path)
            if new_path not in self.cues_by_track:
                self.cues_by_track[new_path] = old_cues
            else:
                existing = self.cues_by_track[new_path]
                fingerprints = {
                    (
                        float(c.get("seconds", 0.0)),
                        str(c.get("label", "")),
                        str(c.get("type", "")),
                    )
                    for c in existing
                }
                for cue in old_cues:
                    key = (
                        float(cue.get("seconds", 0.0)),
                        str(cue.get("label", "")),
                        str(cue.get("type", "")),
                    )
                    if key not in fingerprints:
                        existing.append(cue)
                        fingerprints.add(key)
                existing.sort(key=lambda c: float(c.get("seconds", 0.0)))

        for mapping_name in ("standby_preview_cache", "_playlist_preload_state"):
            mapping = getattr(self, mapping_name, None)
            if isinstance(mapping, dict) and old_path in mapping:
                mapping[new_path] = mapping.pop(old_path)

        pending = getattr(self, "_playlist_preload_pending", None)
        if isinstance(pending, set) and old_path in pending:
            pending.discard(old_path)
            pending.add(new_path)

        queue = getattr(self, "_playlist_preload_queue", None)
        if isinstance(queue, list):
            for index, value in enumerate(queue):
                if value == old_path:
                    queue[index] = new_path

        for attr in ("standby_path", "_standby_preloaded_path"):
            if str(getattr(self, attr, "") or "") == old_path:
                setattr(self, attr, new_path)

    def relink_playlist_row_to_path(self, row: int, new_path: str) -> bool:
        if row < 0 or row >= self.playlist.count():
            return False
        candidate = Path(new_path).expanduser()
        if (
            not candidate.exists()
            or not candidate.is_file()
            or candidate.suffix.lower() not in AUDIO_EXTENSIONS
        ):
            self.show_error("Selected file is not a supported audio file.")
            return False

        item = self.playlist.item(row)
        if item is None:
            return False
        old_path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        resolved = str(candidate.resolve())

        self._migrate_track_path_state(old_path, resolved)
        item.setData(Qt.ItemDataRole.UserRole, resolved)
        item.setData(PLAYLIST_MISSING_ROLE, False)
        meta = self._safe_media_metadata(candidate)
        current_project = str(getattr(self, "_current_project_path", "") or "")
        meta["relative_path"] = self._relative_media_path(resolved, current_project)
        meta["original_path"] = old_path
        item.setData(PLAYLIST_MEDIA_META_ROLE, meta)
        item.setData(PLAYLIST_PROGRESS_ROLE, 0)

        offset_item = self.playlist.item(row, self.playlist.OFFSET_COLUMN)
        if offset_item is not None:
            offset_item.setData(Qt.ItemDataRole.UserRole, resolved)

        self.refresh_playlist_item_caption(item)
        self.refresh_playlist_offset_cells()
        self.queue_playlist_preload([resolved], prioritize=[resolved])
        if hasattr(self, "mark_project_dirty"):
            self.mark_project_dirty()
        self.set_status(f"MEDIA RELINKED — {candidate.name}")
        return True

    def relink_single_playlist_row(self, row: int | None = None) -> bool:
        if row is None or row < 0 or row >= self.playlist.count():
            selected = self.playlist.currentRow()
            row = (
                selected
                if selected >= 0
                else (self.missing_playlist_rows()[0] if self.missing_playlist_rows() else -1)
            )
        if row < 0:
            return False

        item = self.playlist.item(row)
        if item is None:
            return False
        meta = item.data(PLAYLIST_MEDIA_META_ROLE)
        metadata = dict(meta) if isinstance(meta, dict) else {}
        filename = str(
            metadata.get("filename", "")
            or self._portable_filename(str(item.data(Qt.ItemDataRole.UserRole) or ""))
        )
        filter_text = (
            "Audio Files ("
            + " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS))
            + ");;All Files (*.*)"
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Relink Media — {filename}",
            "",
            filter_text,
        )
        if not path:
            return False
        return self.relink_playlist_row_to_path(row, path)

    def _find_best_folder_match(
        self, candidates: list[Path], metadata: dict[str, object]
    ) -> Path | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        scored: list[tuple[int, float, Path]] = []
        expected_duration = float(metadata.get("duration", 0.0) or 0.0)
        for candidate in candidates:
            score = self._candidate_matches_metadata(candidate, metadata)
            duration_delta = 999999.0
            if expected_duration > 0:
                info = self._safe_media_metadata(candidate)
                actual_duration = float(info.get("duration", 0.0) or 0.0)
                if actual_duration > 0:
                    duration_delta = abs(actual_duration - expected_duration)
                    if duration_delta <= 0.25:
                        score += 50
                    elif duration_delta <= 1.0:
                        score += 20
            scored.append((score, -duration_delta, candidate))

        scored.sort(key=lambda value: (value[0], value[1]), reverse=True)
        if len(scored) == 1 or scored[0][:2] > scored[1][:2]:
            return scored[0][2]
        return None

    def relink_missing_media_from_folder(self, folder: str | None = None) -> tuple[int, int]:
        missing_rows = self.missing_playlist_rows()
        if not missing_rows:
            self.set_status("MEDIA OK — NO MISSING FILES")
            return 0, 0

        if not folder:
            folder = QFileDialog.getExistingDirectory(self, "Relink Media Folder")
        if not folder:
            return 0, len(missing_rows)

        root = Path(folder).expanduser()
        by_name: dict[str, list[Path]] = {}
        try:
            for candidate in root.rglob("*"):
                if candidate.is_file() and candidate.suffix.lower() in AUDIO_EXTENSIONS:
                    by_name.setdefault(candidate.name.lower(), []).append(candidate)
        except Exception as exc:
            self.show_error(f"Relink Media: {exc}")
            return 0, len(missing_rows)

        relinked = 0
        # Rows remain stable because relinking does not insert/remove rows.
        for row in missing_rows:
            item = self.playlist.item(row)
            if item is None:
                continue
            raw_meta = item.data(PLAYLIST_MEDIA_META_ROLE)
            metadata = dict(raw_meta) if isinstance(raw_meta, dict) else {}
            filename = str(
                metadata.get("filename", "")
                or self._portable_filename(str(item.data(Qt.ItemDataRole.UserRole) or ""))
            )
            matches = by_name.get(filename.lower(), [])
            candidate = self._find_best_folder_match(matches, metadata)
            if candidate is not None and self.relink_playlist_row_to_path(row, str(candidate)):
                relinked += 1

        remaining = len(self.missing_playlist_rows())
        self.set_status(f"RELINK MEDIA — {relinked} FOUND / {remaining} MISSING")
        return relinked, remaining

    def relink_missing_media_dialog(self) -> None:
        rows = self.missing_playlist_rows()
        if not rows:
            QMessageBox.information(self, "Relink Media", "All project media is available.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Missing Media")
        dialog.setModal(True)
        dialog.setMinimumWidth(560)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        title = QLabel(f"{len(rows)} audio file(s) are missing")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        names = []
        for row in rows[:8]:
            item = self.playlist.item(row)
            if item is not None:
                names.append("• " + self.playlist_display_title_for_item(item))
        if len(rows) > 8:
            names.append(f"• … and {len(rows) - 8} more")
        detail = QLabel("\n".join(names))
        detail.setWordWrap(True)
        layout.addWidget(detail)

        help_text = QLabel(
            "Relink File locates one track. Relink Folder scans a folder and its "
            "subfolders and matches media by filename, file size, and duration. "
            "Cue and Offset data are kept while media is missing."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        buttons = QHBoxLayout()
        file_button = QPushButton("Relink File")
        folder_button = QPushButton("Relink Folder")
        close_button = QPushButton("Later")
        buttons.addWidget(file_button)
        buttons.addWidget(folder_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        def relink_one() -> None:
            row = self.playlist.currentRow()
            if row not in self.missing_playlist_rows():
                row = self.missing_playlist_rows()[0]
            if self.relink_single_playlist_row(row):
                dialog.accept()

        def relink_folder() -> None:
            _count, remaining = self.relink_missing_media_from_folder()
            if remaining == 0:
                dialog.accept()
            else:
                dialog.accept()
                QTimer.singleShot(0, self.relink_missing_media_dialog)

        file_button.clicked.connect(relink_one)
        folder_button.clicked.connect(relink_folder)
        close_button.clicked.connect(dialog.reject)
        dialog.exec()
