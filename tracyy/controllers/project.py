from __future__ import annotations

import hashlib
import itertools
import json
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from tracyy.core import tracyy_app_root
from tracyy.core.ids import cue_key, new_origin, track_key
from tracyy.widgets import normalize_marker_shape

TRACYY_PROJECT_SUFFIX = ".Tracyy"
TRACYY_PROJECT_FILTER = "Tracyy Project (*.Tracyy)"
TRACYY_LEGACY_PROJECT_FILTER = "Legacy Tracyy Project (*.json)"
TRACYY_OPEN_PROJECT_FILTER = (
    f"{TRACYY_PROJECT_FILTER};;{TRACYY_LEGACY_PROJECT_FILTER};;All Files (*.*)"
)

TRACYY_BACKUP_INTERVAL_MS = 15 * 60 * 1000
TRACYY_BACKUP_FOLDER_NAME = "Backup"


def normalize_tracyy_project_save_path(path: str) -> str:
    """Return a save path using the canonical .Tracyy extension.

    Legacy .json names are upgraded by replacing the suffix rather than
    producing names such as ``show.json.Tracyy``. Other suffixes are kept
    and the Tracyy suffix is appended so explicit user filenames are not
    silently truncated.
    """
    normalized = str(Path(path).expanduser()).strip()
    if not normalized:
        return ""

    target = Path(normalized)
    if target.suffix.lower() == ".tracyy":
        # Normalize extension casing for newly saved project files.
        return str(target.with_suffix(TRACYY_PROJECT_SUFFIX))
    if target.suffix.lower() == ".json":
        return str(target.with_suffix(TRACYY_PROJECT_SUFFIX))
    if not target.suffix:
        return str(target.with_suffix(TRACYY_PROJECT_SUFFIX))
    return normalized + TRACYY_PROJECT_SUFFIX


class ProjectControllerMixin:
    def mark_project_dirty(self) -> None:
        """Record that project state changed without writing sidecar files.

        Dirty detection still uses the project-state signature.  The revision is
        useful for UI/backup bookkeeping and keeps controller call sites cheap.
        """
        self._project_dirty_revision = int(getattr(self, "_project_dirty_revision", 0)) + 1

    def restore_auto_backup_preference(self) -> None:
        settings = QSettings("Tracyy", "Tracyy")
        enabled = bool(settings.value("project/auto_backup_15m", False, type=bool))
        checkbox = getattr(self, "auto_backup_checkbox", None)
        if checkbox is not None:
            checkbox.blockSignals(True)
            checkbox.setChecked(enabled)
            checkbox.blockSignals(False)
        self.set_auto_backup_enabled(enabled, persist=False, announce=False)

    def set_auto_backup_enabled(
        self,
        enabled: bool,
        *,
        persist: bool = True,
        announce: bool = True,
    ) -> None:
        enabled = bool(enabled)
        if persist:
            settings = QSettings("Tracyy", "Tracyy")
            settings.setValue("project/auto_backup_15m", enabled)

        timer = getattr(self, "_project_backup_timer", None)
        if timer is not None:
            if enabled:
                timer.start(TRACYY_BACKUP_INTERVAL_MS)
            else:
                timer.stop()

        if enabled:
            current = str(getattr(self, "_current_project_path", "") or "").strip()
            if current:
                try:
                    self.project_backup_directory().mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
                if announce:
                    self.set_status("AUTO BACKUP ON — EVERY 15 MIN")
            elif announce:
                self.set_status("AUTO BACKUP ON — SAVE PROJECT FIRST")
        elif announce:
            self.set_status("AUTO BACKUP OFF")

    def on_auto_backup_toggled(self, checked: bool) -> None:
        self.set_auto_backup_enabled(bool(checked), persist=True)

    def project_backup_directory(self) -> Path:
        current = str(getattr(self, "_current_project_path", "") or "").strip()
        if not current:
            raise ValueError("Save the Tracyy project before creating backups.")
        return Path(current).expanduser().resolve().parent / TRACYY_BACKUP_FOLDER_NAME

    def _atomic_write_project_payload(self, target: Path, payload: dict[str, object]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(temporary, target)

    def write_project_backup(self) -> bool:
        checkbox = getattr(self, "auto_backup_checkbox", None)
        if checkbox is not None and not checkbox.isChecked():
            return False

        current = str(getattr(self, "_current_project_path", "") or "").strip()
        if not current:
            self.set_status("BACKUP WAITING — SAVE PROJECT FIRST")
            return False

        try:
            current_path = Path(current).expanduser().resolve()
            backup_dir = self.project_backup_directory()
            backup_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            backup_name = f"{current_path.stem}__{timestamp}{TRACYY_PROJECT_SUFFIX}"
            backup_path = backup_dir / backup_name
            self._atomic_write_project_payload(
                backup_path, self.build_project_payload(current_path)
            )
            self.set_status(f"PROJECT BACKUP — {backup_path.name}")
            return True
        except Exception as exc:
            self.set_status(f"PROJECT BACKUP ERROR — {exc}")
            return False

    @staticmethod
    def _legacy_track_offsets() -> dict[str, int]:
        """Read the pre-V1.2.5 sidecar once for legacy project migration."""
        legacy_path = tracyy_app_root() / "track_offsets.json"
        if not legacy_path.exists():
            return {}
        try:
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
            return {str(key): int(value) for key, value in dict(raw).items()}
        except Exception:
            return {}

    def project_state_signature(
        self,
    ) -> str:
        payload = self.build_project_payload()
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def mark_project_clean(
        self,
    ) -> None:
        try:
            self._saved_project_signature = self.project_state_signature()
        except Exception:
            # A clean baseline should only be recorded from a valid,
            # serializable project state.
            self._saved_project_signature = ""

    def project_has_unsaved_changes(
        self,
    ) -> bool:
        try:
            current_signature = self.project_state_signature()
        except Exception:
            # Fail safe: if the state cannot be compared, show the prompt
            # instead of silently discarding possible changes.
            return True

        saved_signature = str(self._saved_project_signature or "")
        if not saved_signature:
            return True

        return current_signature != saved_signature

    def track_id_for_path(self, path: str) -> str:
        """The id of the song at ``path``, minting one the first time.

        Lazy on purpose: a track enters the playlist from a file drop, a
        project load, a relink and an import, and hooking all four would leave
        the fifth one silently without an id. Asking here means every route
        gets one at the moment it is first needed.
        """
        key = str(path or "").strip()
        if not key:
            return ""
        existing = self.track_ids.get(key)
        if existing:
            return existing
        minted = self.mint_track_id()
        self.track_ids[key] = minted
        return minted

    def cue_document_key(self, key: str) -> str:
        """The track id a bucket of cues is written under.

        While a project is open the buckets are keyed by local path, with one
        exception: cues belonging to a song this machine does not have were
        parked under their track id when the file was read. Those go back out
        under that same id — minting a new one for them would detach the cues
        from their song for everyone else in the session, which is the exact
        failure this whole change exists to prevent.
        """
        text = str(key or "")
        if text in self._orphan_cue_track_ids:
            return track_key(text)
        return self.track_id_for_path(text)

    def path_for_track_id(self, track_id: str) -> str:
        """Where that song lives on *this* machine, or empty if it is not here."""
        wanted = track_key(track_id)
        if not wanted:
            return ""
        for path, assigned in self.track_ids.items():
            if assigned == wanted:
                return path
        return ""

    def adopt_track_id(self, path: str, track_id: str) -> str:
        """Bind an id that came from a project file or another machine."""
        key = str(path or "").strip()
        wanted = track_key(track_id)
        if not key or not wanted:
            return self.track_id_for_path(key) if key else ""
        self.track_ids[key] = wanted
        return wanted

    def build_project_payload(
        self,
        project_path: str | Path | None = None,
    ) -> dict[str, object]:
        project_context = (
            project_path or str(getattr(self, "_current_project_path", "") or "") or None
        )
        return {
            "format": "Tracyy Project",
            # 4 keys cues by track id instead of by path, and gives every
            # playlist entry an id. A v3 file still loads; see load_project_path.
            "version": 4,
            "project_fps": int(self.fps.currentText()),
            "track_offsets": {
                str(path): int(frames) for path, frames in self.track_offsets.items()
            },
            "playlist": [
                self.project_media_entry_for_item(
                    self.playlist.item(index),
                    project_context,
                )
                for index in range(self.playlist.count())
                if self.playlist.item(index) is not None
            ],
            "cues": {
                self.cue_document_key(key): cues
                for key, cues in self.cues_by_track.items()
                if self.cue_document_key(key)
            },
            "cue_types": [
                {
                    "name": name,
                    "color": color,
                    "visible": bool(
                        self.cue_type_meta.get(
                            name,
                            {},
                        ).get(
                            "visible",
                            True,
                        )
                    ),
                    "locked": bool(
                        self.cue_type_meta.get(
                            name,
                            {},
                        ).get(
                            "locked",
                            False,
                        )
                    ),
                    "sync": bool(
                        self.cue_type_meta.get(
                            name,
                            {},
                        ).get(
                            "sync",
                            False,
                        )
                    ),
                    "marker_shape": (
                        normalize_marker_shape(
                            self.cue_type_meta.get(
                                name,
                                {},
                            ).get(
                                "marker_shape",
                                "rectangle",
                            )
                        )
                    ),
                }
                for name, color in self.cue_type_defs
            ],
            "input_timecode_settings": {
                "fps": (self.input_timecode_selected_fps()),
                "latency_ms": (self.input_timecode_latency_ms_value()),
                "pre_roll_seconds": (self.input_timecode_pre_roll_seconds()),
                "after_roll_seconds": (self.input_timecode_after_roll_seconds()),
            },
            "output_devices": (self.output_device_config_snapshot()),
        }

    def write_project_file(
        self,
        path: str,
    ) -> bool:
        normalized = normalize_tracyy_project_save_path(path)
        if not normalized:
            return False

        try:
            target = Path(normalized)
            target.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            payload = self.build_project_payload(target)
            self._atomic_write_project_payload(target, payload)

            self._current_project_path = str(target.resolve())
            if bool(
                getattr(self, "auto_backup_checkbox", None)
                and self.auto_backup_checkbox.isChecked()
            ):
                self.project_backup_directory().mkdir(parents=True, exist_ok=True)
                self._project_backup_timer.start(TRACYY_BACKUP_INTERVAL_MS)
            self.mark_project_clean()
            self.set_status(f"PROJECT SAVED — {target.name}")
            return True
        except Exception as exc:
            self.show_error(str(exc))
            return False

    def save_project_for_close(
        self,
    ) -> bool:
        current_path = str(self._current_project_path or "").strip()

        if current_path:
            return self.write_project_file(current_path)

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Tracyy Project",
            "tracyy_project.Tracyy",
            TRACYY_PROJECT_FILTER,
        )
        if not path:
            return False

        return self.write_project_file(path)

    def confirm_project_close(
        self,
    ) -> str:
        dialog = QDialog(self)
        dialog.setObjectName("projectCloseDialog")
        dialog.setWindowTitle("Close Tracyy")
        dialog.setModal(True)
        dialog.setMinimumWidth(620)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(
            36,
            32,
            36,
            28,
        )
        layout.setSpacing(20)

        title = QLabel("Do you want to save this project?")
        title.setObjectName("projectCloseTitle")
        title.setWordWrap(True)

        message = QLabel("If you don't save, any changes will be lost.")
        message.setObjectName("projectCloseMessage")
        message.setWordWrap(True)

        layout.addWidget(title)
        layout.addWidget(message)
        layout.addSpacing(58)

        button_row = QHBoxLayout()
        button_row.setSpacing(24)

        dont_save_button = QPushButton("Don't Save")
        dont_save_button.setObjectName("projectDontSaveButton")
        dont_save_button.setMinimumSize(
            176,
            54,
        )

        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("projectCancelButton")
        cancel_button.setMinimumSize(
            176,
            54,
        )

        save_button = QPushButton("Save and Close")
        save_button.setObjectName("projectSaveCloseButton")
        save_button.setMinimumSize(
            190,
            54,
        )
        save_button.setDefault(True)

        button_row.addWidget(dont_save_button)
        button_row.addStretch(1)
        button_row.addWidget(cancel_button)
        button_row.addWidget(save_button)
        layout.addLayout(button_row)

        dialog.setStyleSheet(
            """
            QDialog#projectCloseDialog {
                background: #1f1f1f;
                border: 1px solid #8b8b8b;
                border-radius: 18px;
            }
            QLabel#projectCloseTitle {
                color: #f2f2f2;
                font-size: 27px;
                font-weight: 700;
                background: transparent;
            }
            QLabel#projectCloseMessage {
                color: #e4e4e4;
                font-size: 20px;
                background: transparent;
            }
            QPushButton#projectDontSaveButton,
            QPushButton#projectCancelButton,
            QPushButton#projectSaveCloseButton {
                border: none;
                border-radius: 19px;
                padding: 10px 18px;
                color: #f1f1f1;
                font-size: 18px;
                font-weight: 600;
            }
            QPushButton#projectDontSaveButton {
                background: #7e3b39;
            }
            QPushButton#projectDontSaveButton:hover {
                background: #914744;
            }
            QPushButton#projectCancelButton {
                background: #353535;
            }
            QPushButton#projectCancelButton:hover {
                background: #444444;
            }
            QPushButton#projectSaveCloseButton {
                background: #34536d;
            }
            QPushButton#projectSaveCloseButton:hover {
                background: #406682;
            }
            """
        )

        dont_save_button.clicked.connect(lambda: dialog.done(1))
        cancel_button.clicked.connect(lambda: dialog.done(0))
        save_button.clicked.connect(lambda: dialog.done(2))

        result = dialog.exec()
        if result == 1:
            return "discard"
        if result == 2:
            return "save"
        return "cancel"

    def perform_application_shutdown(
        self,
    ) -> None:
        if self._shutdown_in_progress:
            return

        try:
            self.license_client.usb_admin.prepare_shutdown(self._usb_runtime_state)
        except Exception:
            pass

        self._shutdown_in_progress = True
        self._license_connectivity_timer.stop()
        self._license_expiry_timer.stop()
        self._usb_admin_timer.stop()
        self._license_display_timer.stop()
        try:
            self._project_backup_timer.stop()
        except Exception:
            pass

        self.stop_input_timecode_engine()
        self.stop_all_server_profiles()
        # Leave the live session too, so the people still in it see this
        # machine go rather than waiting out its timeout.
        self.shutdown_sync()

        for profile in self.server_profiles:
            try:
                Path(profile["state_path"]).unlink(missing_ok=True)
            except Exception:
                pass

        try:
            self.media_workers.shutdown(wait=False)
        except Exception:
            pass

        self.transport.close()

    def save_project(self) -> bool:
        suggested_path = (
            normalize_tracyy_project_save_path(self._current_project_path)
            if self._current_project_path
            else "tracyy_project.Tracyy"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Tracyy Project",
            suggested_path,
            TRACYY_PROJECT_FILTER,
        )
        if not path:
            return False

        return self.write_project_file(path)

    def load_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Tracyy Project",
            "",
            TRACYY_OPEN_PROJECT_FILTER,
        )
        if path:
            self.load_project_path(path)

    def load_project_path(self, path: str) -> bool:
        project_path = Path(str(path)).expanduser()
        if not project_path.exists() or not project_path.is_file():
            self.show_error(f"Project not found: {project_path}")
            return False

        if project_path.suffix.lower() not in {".tracyy", ".json"}:
            self.show_error("Unsupported Tracyy project file.")
            return False

        path = str(project_path)

        try:
            project = json.loads(project_path.read_text(encoding="utf-8"))

            # Project FPS and offsets are part of the single .Tracyy source of truth.
            # Older projects did not contain offsets, so import the legacy sidecar
            # once when available; the next Save upgrades everything into .Tracyy.
            try:
                loaded_project_fps = int(
                    project.get("project_fps", int(self.fps.currentText()))
                )
            except Exception:
                loaded_project_fps = int(self.fps.currentText())
            if loaded_project_fps not in {24, 25, 30}:
                loaded_project_fps = int(self.fps.currentText())

            self.track_offsets = {}
            self.fps.blockSignals(True)
            try:
                self.fps.setCurrentText(str(loaded_project_fps))
            finally:
                self.fps.blockSignals(False)
            self._input_timecode_project_fps = loaded_project_fps

            raw_offsets = project.get("track_offsets", None)
            if isinstance(raw_offsets, dict):
                self.track_offsets = {
                    str(key): max(0, int(value)) for key, value in raw_offsets.items()
                }
            else:
                self.track_offsets = self._legacy_track_offsets()

            input_settings = project.get(
                "input_timecode_settings",
                {},
            )
            if isinstance(
                input_settings,
                dict,
            ):
                loaded_input_fps = str(
                    input_settings.get(
                        "fps",
                        self._input_timecode_fps_default,
                    )
                )
                if self.input_timecode_fps.findText(loaded_input_fps) >= 0:
                    self.input_timecode_fps.setCurrentText(loaded_input_fps)

                self.input_timecode_latency_ms.setValue(
                    max(
                        -5000.0,
                        min(
                            5000.0,
                            float(
                                input_settings.get(
                                    "latency_ms",
                                    self._input_timecode_latency_ms_default,
                                )
                            ),
                        ),
                    )
                )

                self.input_timecode_pre_roll.setValue(
                    max(
                        0.0,
                        float(
                            input_settings.get(
                                "pre_roll_seconds",
                                self._input_timecode_pre_roll_default,
                            )
                        ),
                    )
                )
                self.input_timecode_after_roll.setValue(
                    max(
                        0.0,
                        float(
                            input_settings.get(
                                "after_roll_seconds",
                                self._input_timecode_after_roll_default,
                            )
                        ),
                    )
                )
                self.on_input_timecode_roll_changed()

            output_settings = project.get(
                "output_devices",
                [],
            )
            if (
                isinstance(
                    output_settings,
                    list,
                )
                and output_settings
            ):
                self.restore_output_device_configs(output_settings)

            playlist_entries: list[dict[str, object]] = []
            media_path_map: dict[str, str] = {}

            for raw_entry in project.get("playlist", []):
                if isinstance(raw_entry, dict):
                    source_entry = dict(raw_entry)
                    entry_path = str(source_entry.get("path", "") or "").strip()
                    display_name = str(source_entry.get("display_name", "") or "").strip()
                else:
                    # Backward compatibility with pre-V1.2.7 project files.
                    entry_path = str(raw_entry or "").strip()
                    display_name = ""
                    source_entry = {"path": entry_path}

                if not entry_path:
                    continue

                resolved_path, missing, metadata = self.resolve_project_media_entry(
                    project_path,
                    source_entry,
                )
                resolved_path = str(resolved_path or entry_path).strip()
                if not display_name:
                    display_name = str(
                        source_entry.get("filename", "") or self._portable_filename(entry_path)
                    )

                playlist_entries.append(
                    {
                        "path": resolved_path,
                        "original_path": entry_path,
                        "track_id": str(source_entry.get("track_id", "") or ""),
                        "display_name": display_name,
                        "missing": bool(missing),
                        "metadata": metadata,
                    }
                )
                if resolved_path and resolved_path != entry_path:
                    media_path_map[entry_path] = resolved_path

            # Bind ids before the cues are read: from version 4 the cue table
            # is keyed by track id, and resolving those needs this map. A v3
            # file has no ids, so every track gets a fresh one here and the
            # document comes out of this load already migrated.
            self.track_ids = {}
            self._orphan_cue_track_ids = set()
            for entry in playlist_entries:
                local_path = str(entry.get("path", "") or "")
                if not local_path:
                    continue
                stored_id = str(entry.get("track_id", "") or "")
                if stored_id:
                    self.adopt_track_id(local_path, stored_id)
                else:
                    self.track_id_for_path(local_path)

            if media_path_map:
                remapped_offsets: dict[str, int] = {}
                for old_key, value in self.track_offsets.items():
                    remapped_offsets[media_path_map.get(str(old_key), str(old_key))] = int(
                        value
                    )
                self.track_offsets = remapped_offsets

            raw_types = project.get("cue_types", [])
            if isinstance(raw_types, list) and raw_types:
                loaded_types = []
                loaded_meta = {}
                for entry in raw_types:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("name", "")).strip()
                    color = QColor(str(entry.get("color", "#ffb020")))
                    if name and color.isValid():
                        loaded_types.append((name, color.name()))
                        loaded_meta[name] = {
                            "visible": bool(entry.get("visible", True)),
                            "locked": bool(entry.get("locked", False)),
                            "sync": bool(entry.get("sync", False)),
                            "marker_shape": normalize_marker_shape(
                                entry.get(
                                    "marker_shape",
                                    "rectangle",
                                )
                            ),
                        }
                if loaded_types:
                    self.cue_type_defs = loaded_types
                    self.cue_type_meta = loaded_meta
                    self.ensure_cue_type_meta()
                    self.active_cue_type = loaded_types[0][0]
                    self.rebuild_cue_type_buttons()

            raw_cues = project.get("cues", {})

            # From version 4 the cue table is keyed by track id. Older files
            # are keyed by the author's paths, which is exactly what made the
            # same show unshareable between two machines — so they are
            # translated here, once, and saved back in the new shape.
            try:
                file_version = int(project.get("version", 1) or 1)
            except (TypeError, ValueError):
                file_version = 1

            if file_version >= 4:
                by_id = {track_key(assigned): path for path, assigned in self.track_ids.items()}
                translated: dict[str, object] = {}
                for stored_key, cues in raw_cues.items():
                    local_path = by_id.get(track_key(stored_key), "")
                    if local_path:
                        translated[local_path] = cues
                    else:
                        # A track id with no song on this machine: the entry
                        # was removed from the playlist, or the file arrived
                        # from a machine that has a song this one does not.
                        # Dropping the cues silently would lose somebody's
                        # work, so they are parked under the id itself and
                        # travel back out on the next save.
                        parked = track_key(stored_key)
                        translated[parked] = cues
                        self._orphan_cue_track_ids.add(parked)
                raw_cues = translated

            # A v3 cue id is an integer that only means anything inside its own
            # track. Rewriting them to document-wide ids is the migration: it
            # is what stops two editors minting the same number later.
            migration_origin = new_origin(6)
            migration_counter = itertools.count(1)

            self.cues_by_track = {
                str(track): [
                    {
                        "id": (
                            cue_key(cue.get("id"))
                            if file_version >= 4 and cue_key(cue.get("id"))
                            else f"{migration_origin}-{next(migration_counter)}"
                        ),
                        "seconds": float(cue.get("seconds", 0.0)),
                        "name": str(cue.get("name", f"Cue {index + 1}")),
                        "label": str(cue.get("label", "")),
                        "type": str(cue.get("type", "Choreo")),
                        "color": str(
                            cue.get(
                                "color",
                                self.cue_type_color(str(cue.get("type", "Choreo"))),
                            )
                        ),
                    }
                    for index, cue in enumerate(cues)
                ]
                for track, cues in raw_cues.items()
                if isinstance(cues, list)
            }

            if media_path_map:
                remapped_cues: dict[str, list[dict[str, object]]] = {}
                for old_track, cue_list in self.cues_by_track.items():
                    target_track = media_path_map.get(str(old_track), str(old_track))
                    if target_track not in remapped_cues:
                        remapped_cues[target_track] = list(cue_list)
                    else:
                        remapped_cues[target_track].extend(cue_list)
                        remapped_cues[target_track].sort(
                            key=lambda cue: float(cue.get("seconds", 0.0))
                        )
                self.cues_by_track = remapped_cues

            self.playlist.clear()
            available_paths: list[str] = []
            first_available_row = -1
            for row, entry in enumerate(playlist_entries):
                entry_path = str(entry.get("path", "") or "").strip()
                if not entry_path:
                    continue
                missing = bool(entry.get("missing", False))
                self.add_project_media_item(
                    entry_path,
                    str(entry.get("display_name", "") or ""),
                    missing=missing,
                    metadata=dict(entry.get("metadata", {}) or {}),
                )
                if not missing:
                    available_paths.append(entry_path)
                    if first_available_row < 0:
                        first_available_row = row

            if available_paths:
                self.queue_playlist_preload(
                    available_paths,
                    prioritize=available_paths[:2],
                )

            self._current_project_path = str(Path(path).resolve())
            if bool(
                getattr(self, "auto_backup_checkbox", None)
                and self.auto_backup_checkbox.isChecked()
            ):
                self.project_backup_directory().mkdir(parents=True, exist_ok=True)
                self._project_backup_timer.start(TRACYY_BACKUP_INTERVAL_MS)
            self.refresh_playlist_offset_cells()
            self.update_main_offset_ui()

            if first_available_row >= 0:
                self.playlist.setCurrentRow(first_available_row)
                item = self.playlist.item(first_available_row)
                if item is not None:
                    self.select_playlist_item(item)

            self.mark_project_clean()
            missing_count = len(self.missing_playlist_rows())
            if missing_count:
                self.set_status(f"PROJECT LOADED — {missing_count} MEDIA MISSING")
                QTimer.singleShot(0, self.relink_missing_media_dialog)
            else:
                self.set_status(f"PROJECT LOADED — {Path(path).name}")
            return True
        except Exception as exc:
            self.show_error(str(exc))
            return False
