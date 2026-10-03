from __future__ import annotations

import copy
from bisect import bisect_right

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QColorDialog,
    QComboBox,
    QInputDialog,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from tracyy.core.ids import cue_key, new_cue_id, new_track_id
from tracyy.widgets import (
    CueTypeEditorDialog,
    frames_to_timecode,
    normalize_marker_shape,
    timecode_to_frames,
)


class CueControllerMixin:
    def ensure_cue_type_meta(self) -> None:
        names = [name for name, _color in self.cue_type_defs]
        for name in names:
            meta = self.cue_type_meta.setdefault(
                name,
                {
                    "visible": True,
                    "locked": False,
                    "sync": False,
                    "marker_shape": "rectangle",
                },
            )
            meta.setdefault(
                "visible",
                True,
            )
            meta.setdefault(
                "locked",
                False,
            )
            meta.setdefault(
                "sync",
                False,
            )
            meta["marker_shape"] = normalize_marker_shape(
                meta.get(
                    "marker_shape",
                    "rectangle",
                )
            )

        for existing in list(self.cue_type_meta):
            if existing not in names:
                del self.cue_type_meta[existing]

    def cue_type_is_locked(self, cue_type: str) -> bool:
        self.ensure_cue_type_meta()
        return bool(self.cue_type_meta.get(cue_type, {}).get("locked", False))

    def cue_type_is_visible(self, cue_type: str) -> bool:
        self.ensure_cue_type_meta()
        return bool(self.cue_type_meta.get(cue_type, {}).get("visible", True))

    def open_cue_type_editor(self) -> None:
        if not self.require_cue_edit_rights():
            return
        dialog = CueTypeEditorDialog(self)
        dialog.exec()

    def cue_type_marker_shape(
        self,
        cue_type: str,
    ) -> str:
        self.ensure_cue_type_meta()
        return normalize_marker_shape(
            self.cue_type_meta.get(
                cue_type,
                {},
            ).get(
                "marker_shape",
                "rectangle",
            )
        )

    def set_cue_type_marker_shape(
        self,
        cue_type: str,
        marker_shape: str,
    ) -> None:
        if not self.require_cue_edit_rights():
            return
        self.ensure_cue_type_meta()
        normalized = normalize_marker_shape(marker_shape)
        self.cue_type_meta.setdefault(
            cue_type,
            {},
        )["marker_shape"] = normalized
        self.refresh_waveform_markers()
        self.set_status(f"MARKER SHAPE — {cue_type} — {normalized.upper()}")

    def set_cue_type_visible(self, cue_type: str, visible: bool) -> None:
        if not self.require_cue_edit_rights():
            return
        self.ensure_cue_type_meta()
        self.cue_type_meta.setdefault(cue_type, {})["visible"] = visible

        # Visible only controls waveform markers and Loading.
        # Cue Table intentionally remains complete.
        self.refresh_waveform_markers()
        self.update_next_cue_loading(self.transport.current_position())

    def set_cue_type_locked(self, cue_type: str, locked: bool) -> None:
        if not self.require_cue_edit_rights():
            return
        self.ensure_cue_type_meta()
        self.cue_type_meta.setdefault(cue_type, {})["locked"] = locked
        self.refresh_waveform_markers()

    def set_cue_type_sync(self, cue_type: str, sync_value: bool) -> None:
        if not self.require_cue_edit_rights():
            return
        self.ensure_cue_type_meta()
        self.cue_type_meta.setdefault(cue_type, {})["sync"] = sync_value

    def delete_cue_type(self, cue_type: str) -> None:
        if not self.require_cue_edit_rights():
            return
        if self.cue_type_is_locked(cue_type):
            self.show_error("Cue type này đang bị khóa.")
            return

        if len(self.cue_type_defs) <= 1:
            self.show_error("Phải giữ lại ít nhất 1 cue type.")
            return

        affected_count = sum(
            1
            for track_cues in self.cues_by_track.values()
            for cue in track_cues
            if str(cue.get("type", "")) == cue_type
        )

        self.push_cue_undo(
            f"DELETE CUE TYPE {cue_type}",
            full_state=True,
        )

        remaining = [(name, color) for name, color in self.cue_type_defs if name != cue_type]
        fallback = remaining[0][0]

        self.cue_type_defs = remaining
        self.cue_type_meta.pop(cue_type, None)

        for track_cues in self.cues_by_track.values():
            track_cues[:] = [cue for cue in track_cues if str(cue.get("type", "")) != cue_type]

        if self.active_cue_type == cue_type:
            self.active_cue_type = fallback

        self.rebuild_cue_type_buttons()
        self.refresh_cue_table()
        self.save_all_cue_csvs()
        self.update_next_cue_loading(self.transport.current_position())
        self.set_status(f"DELETED CUE TYPE {cue_type} — {affected_count} CUES REMOVED")
        QTimer.singleShot(
            0,
            self.ensure_active_type_visible,
        )

    def _setup_cue_keyboard_shortcuts(self) -> None:
        self.cue_number_shortcuts: list[QShortcut] = []

        for number in range(1, 10):
            shortcut = QShortcut(QKeySequence(str(number)), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(lambda value=number: self.add_cue_from_number_key(value))
            self.cue_number_shortcuts.append(shortcut)

        self.play_pause_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Space),
            self,
        )
        self.play_pause_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.play_pause_shortcut.activated.connect(self.toggle_play_pause_from_keyboard)

    def toggle_play_pause_from_keyboard(self) -> None:
        if self.keyboard_focus_is_editing():
            return

        if self.transport.running:
            self.transport.pause()
        else:
            self.play()

        self.update_play_pause_icon()

    def cue_type_for_number(
        self,
        number: int,
    ) -> tuple[str, str] | None:
        index = int(number) - 1
        if not 0 <= index < len(self.cue_type_defs):
            return None
        return self.cue_type_defs[index]

    def keyboard_focus_is_editing(self) -> bool:
        focus = QApplication.focusWidget()
        if focus is None:
            return False

        if isinstance(focus, QLineEdit):
            return True

        if isinstance(focus, QComboBox):
            return focus.isEditable()

        if isinstance(focus, QTableWidget):
            return focus.state() == QAbstractItemView.State.EditingState

        parent = focus.parentWidget()
        while parent is not None:
            if isinstance(parent, QTableWidget):
                return parent.state() == QAbstractItemView.State.EditingState
            parent = parent.parentWidget()

        return False

    def add_cue_from_number_key(self, number: int) -> None:
        if self.keyboard_focus_is_editing():
            return

        cue_type_entry = self.cue_type_for_number(number)
        if cue_type_entry is None:
            return

        cue_name, _cue_color = cue_type_entry
        self.set_active_cue_type(
            cue_name,
            apply_to_selected=False,
        )

        # add_cue() records transport.current_position(), so pressing
        # the number while music is running marks the exact playhead position.
        self.add_cue()

    def cue_type_color(self, cue_type: str) -> str:
        for name, color in self.cue_type_defs:
            if name == cue_type:
                return color
        return "#ffb020"

    def refresh_cue_type_strip_geometry(self) -> None:
        self.cue_type_content.adjustSize()
        width = max(1, self.cue_type_content.sizeHint().width())
        height = max(34, self.cue_type_content.sizeHint().height())
        self.cue_type_content.setMinimumSize(width, height)
        self.cue_type_content.resize(width, height)
        self.cue_type_content.updateGeometry()
        self.cue_type_scroll.widget().updateGeometry()
        self.cue_type_scroll.viewport().update()

    def ensure_active_type_visible(self) -> None:
        button = self.cue_type_buttons.get(self.active_cue_type)
        if button is None:
            return
        self.cue_type_scroll.ensureWidgetVisible(button, 24, 0)

    def compact_cue_type_text(
        self,
        cue_name: str,
        max_chars: int = 12,
    ) -> str:
        cue_name = str(cue_name)
        if len(cue_name) <= max_chars:
            return cue_name
        return cue_name[: max_chars - 1].rstrip() + "…"

    def rebuild_cue_type_buttons(self) -> None:
        self.ensure_cue_type_meta()

        while self.cue_type_row.count():
            item = self.cue_type_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.cue_type_buttons = {}
        visible_defs = [
            (name, color)
            for name, color in self.cue_type_defs
            if self.cue_type_is_visible(name)
        ]

        if visible_defs and self.active_cue_type not in {name for name, _color in visible_defs}:
            self.active_cue_type = visible_defs[0][0]

        number_lookup = {
            name: index
            for index, (name, _color) in enumerate(
                self.cue_type_defs[:9],
                start=1,
            )
        }

        for cue_name, cue_color in visible_defs:
            compact_name = self.compact_cue_type_text(cue_name, 11)
            assigned_number = number_lookup.get(cue_name)
            display_name = (
                f"{assigned_number} · {compact_name}"
                if assigned_number is not None
                else compact_name
            )
            button = QPushButton(display_name)
            if assigned_number is not None:
                button.setToolTip(f"{cue_name} — Keyboard shortcut: {assigned_number}")
            else:
                button.setToolTip(cue_name)
            button.setMinimumWidth(78)
            button.setMaximumWidth(138)
            button.setFixedHeight(38)
            button.setCheckable(True)
            button.setChecked(cue_name == self.active_cue_type)
            button.setStyleSheet(
                f"""
                QPushButton {{
                    background: transparent;
                    color: {cue_color};
                    border: 1px solid {cue_color};
                    border-radius: 16px;
                    padding: 7px 12px;
                    font-weight: 600;
                }}
                QPushButton:hover {{
                    background: transparent;
                    color: {cue_color};
                    border: 2px solid {cue_color};
                }}
                QPushButton:checked {{
                    background: transparent;
                    color: {cue_color};
                    border: 3px solid {cue_color};
                    padding: 5px 10px;
                }}
                QPushButton:pressed {{
                    background: transparent;
                    color: {cue_color};
                }}
                """
            )
            button.clicked.connect(
                lambda checked=False, name=cue_name: self.add_cue_from_type(name)
            )
            self.cue_type_buttons[cue_name] = button
            self.cue_type_row.addWidget(button)

        spacer = QWidget()
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        spacer.setFixedHeight(1)
        self.cue_type_row.addWidget(spacer)

        self.refresh_cue_type_strip_geometry()
        QTimer.singleShot(0, self.refresh_cue_type_strip_geometry)
        QTimer.singleShot(0, self.ensure_active_type_visible)

    def mint_cue_id(self) -> str:
        """A new cue id, unique across every machine in a session."""
        return new_cue_id(self.id_origin)

    def mint_track_id(self) -> str:
        """A new track id, meaning the same song on every machine."""
        return new_track_id(self.id_origin)

    def push_cue_undo(
        self,
        description: str,
        full_state: bool = False,
    ) -> None:
        if self._restoring_undo:
            return

        track_path = self.main_track_path()
        if full_state or not track_path:
            snapshot = {
                "kind": "full",
                "description": description,
                "cues_by_track": copy.deepcopy(self.cues_by_track),
                "cue_type_defs": copy.deepcopy(self.cue_type_defs),
                "cue_type_meta": copy.deepcopy(self.cue_type_meta),
                "active_cue_type": self.active_cue_type,
            }
        else:
            # Normal cue edits only touch one track. Avoid deepcopying every
            # track in the project for each label/time/marker operation.
            snapshot = {
                "kind": "track",
                "description": description,
                "track_path": track_path,
                "track_cues": copy.deepcopy(self.cues_by_track.get(track_path, [])),
                "active_cue_type": self.active_cue_type,
            }

        self._undo_stack.append(snapshot)
        if len(self._undo_stack) > self._undo_limit:
            del self._undo_stack[0]

    def undo_last_cue_action(self) -> None:
        if not self.require_cue_edit_rights():
            return
        if not self._undo_stack:
            self.set_status("NOTHING TO UNDO")
            return

        snapshot = self._undo_stack.pop()
        self._restoring_undo = True
        try:
            if snapshot.get("kind") == "track":
                track_path = str(snapshot.get("track_path") or "")
                if track_path:
                    self.cues_by_track[track_path] = copy.deepcopy(
                        snapshot.get("track_cues", [])
                    )
                self.active_cue_type = str(
                    snapshot.get(
                        "active_cue_type",
                        self.active_cue_type,
                    )
                )
                if track_path == self.main_track_path():
                    self.refresh_cue_table()
            else:
                self.cues_by_track = copy.deepcopy(snapshot["cues_by_track"])
                self.cue_type_defs = copy.deepcopy(snapshot["cue_type_defs"])
                self.cue_type_meta = copy.deepcopy(snapshot["cue_type_meta"])
                self.active_cue_type = str(snapshot["active_cue_type"])
                self.rebuild_cue_type_buttons()
                self.refresh_cue_table()

            self.save_all_cue_csvs()
            self.update_next_cue_loading(self.transport.current_position())
            self.sync_cue_time_editor_from_selection()
            self.set_status(f"UNDO — {snapshot['description']}")
        finally:
            self._restoring_undo = False

    def set_active_cue_type(
        self,
        cue_type: str,
        apply_to_selected: bool = False,
    ) -> None:
        self.active_cue_type = cue_type
        for name, button in self.cue_type_buttons.items():
            button.setChecked(name == cue_type)

        if apply_to_selected:
            self._complete_pending_cue_table_now()
            cue_id = self.selected_cue_id()
            cue = self.find_cue(cue_id) if cue_id is not None else None
            if cue is not None:
                if not self.require_cue_edit_rights():
                    return
                self.push_cue_undo("CHANGE CUE TYPE")
                cue["type"] = cue_type
                cue["color"] = self.cue_type_color(cue_type)
                self.rebuild_cue_runtime_index(
                    self.current_track_cues(),
                    reset_playback_state=False,
                )
                self._update_single_cue_row(cue_key(cue["id"]))
                self._refresh_waveform_markers_direct()
                self.save_cue_csv()

    def add_cue_from_type(self, cue_type: str) -> None:
        self.set_active_cue_type(cue_type, apply_to_selected=False)
        self.add_cue()

    def sync_active_type_from_selection(self) -> None:
        cue_id = self.selected_cue_id()
        if cue_id is None:
            return

        cue = self.find_cue(cue_id)
        if cue is None:
            return

        cue_type = str(cue.get("type", self.active_cue_type))
        if cue_type in self.cue_type_buttons:
            self.set_active_cue_type(cue_type, apply_to_selected=False)

    def add_cue_type(self) -> None:
        if not self.require_cue_edit_rights():
            return
        name, accepted = QInputDialog.getText(
            self,
            "Add Cue Type",
            "Cue type name:",
        )
        name = name.strip()
        if not accepted or not name:
            return

        if any(existing == name for existing, _ in self.cue_type_defs):
            self.show_error("Cue type này đã tồn tại.")
            return

        color = QColorDialog.getColor(
            QColor("#ffb020"),
            self,
            "Choose Cue Color",
        )
        if not color.isValid():
            return

        if not self.require_cue_edit_rights():
            return
        self.cue_type_defs.append(
            (
                name,
                color.name(),
            )
        )
        self.ensure_cue_type_meta()
        self.cue_type_meta[name] = {
            "visible": True,
            "locked": False,
            "sync": False,
            "marker_shape": "rectangle",
        }
        self.active_cue_type = name
        self.rebuild_cue_type_buttons()
        QTimer.singleShot(
            0,
            self.ensure_active_type_visible,
        )

    def rename_cue_type(self, old_name: str, new_name: str) -> None:
        if not self.require_cue_edit_rights():
            return
        if self.cue_type_is_locked(old_name):
            self.show_error("Cue type này đang bị khóa.")
            return

        updated_defs = []
        for name, color in self.cue_type_defs:
            updated_defs.append((new_name, color) if name == old_name else (name, color))
        self.cue_type_defs = updated_defs

        self.ensure_cue_type_meta()
        self.cue_type_meta[new_name] = self.cue_type_meta.pop(
            old_name,
            {
                "visible": True,
                "locked": False,
                "sync": False,
            },
        )

        for track_cues in self.cues_by_track.values():
            for cue in track_cues:
                if str(cue.get("type", "")) == old_name:
                    cue["type"] = new_name

        if self.active_cue_type == old_name:
            self.active_cue_type = new_name

        self.rebuild_cue_type_buttons()
        self.refresh_cue_table()
        self.save_all_cue_csvs()

    def reorder_cue_types(self, ordered_names: list[str]) -> None:
        if not self.require_cue_edit_rights():
            return
        color_lookup = dict(self.cue_type_defs)

        reordered = [
            (name, color_lookup[name]) for name in ordered_names if name in color_lookup
        ]

        existing_names = {name for name, _color in reordered}
        for name, color in self.cue_type_defs:
            if name not in existing_names:
                reordered.append((name, color))

        self.cue_type_defs = reordered
        self.rebuild_cue_type_buttons()
        QTimer.singleShot(0, self.ensure_active_type_visible)

    def rename_active_cue_type(self) -> None:
        if not self.require_cue_edit_rights():
            return
        old_name = self.active_cue_type
        if self.cue_type_is_locked(old_name):
            self.show_error("Cue type này đang bị khóa.")
            return

        new_name, accepted = QInputDialog.getText(
            self,
            "Rename Cue Type",
            "New cue type name:",
            text=old_name,
        )
        new_name = new_name.strip()
        if not accepted or not new_name or new_name == old_name:
            return

        if any(
            existing == new_name
            for existing, _color in self.cue_type_defs
            if existing != old_name
        ):
            self.show_error("Tên cue type này đã tồn tại.")
            return

        self.rename_cue_type(old_name, new_name)

    def change_active_cue_type_color(self) -> None:
        if not self.require_cue_edit_rights():
            return
        cue_type = self.active_cue_type
        if self.cue_type_is_locked(cue_type):
            self.show_error("Cue type này đang bị khóa.")
            return
        current_color = QColor(self.cue_type_color(cue_type))
        color = QColorDialog.getColor(
            current_color,
            self,
            f"Change Color — {cue_type}",
        )
        if not color.isValid():
            return

        if not self.require_cue_edit_rights():
            return
        new_hex = color.name()
        self.cue_type_defs = [
            (name, new_hex if name == cue_type else existing_color)
            for name, existing_color in self.cue_type_defs
        ]

        for track_cues in self.cues_by_track.values():
            for cue in track_cues:
                if str(cue.get("type", "")) == cue_type:
                    cue["color"] = new_hex

        self.rebuild_cue_type_buttons()
        self.refresh_cue_table()
        self.save_all_cue_csvs()

    def current_cue_timecode(self) -> str:
        fps = int(self.fps.currentText())
        selected_id = self.selected_cue_id()
        if selected_id is not None:
            cue = self.find_cue(selected_id)
            if cue is not None:
                tc_seconds = self.media_to_timecode_seconds(float(cue["seconds"]))
                return frames_to_timecode(
                    round(tc_seconds * fps),
                    fps,
                )

        position = self.transport.current_position()
        tc_seconds = self.media_to_timecode_seconds(position)
        return frames_to_timecode(
            round(tc_seconds * fps),
            fps,
        )

    def sync_cue_time_editor_from_selection_when_stopped(self) -> None:
        if self.transport.running:
            return
        self.sync_cue_time_editor_from_selection()

    def sync_cue_time_editor_from_selection(self) -> None:
        self.refresh_position()

    def apply_cue_time_from_editor(self) -> None:
        return

    def clear_playlist(self) -> None:
        self._ram_ready_paths.clear()
        self._pending_play_after_ram_path = ""
        self.transport.clear_audio_ram_cache()
        self.transport.stop()
        with self.transport._lock:
            self.transport.timecode_offset_seconds = 0.0
            self.transport._position_revision += 1
        self.playlist.clear()
        self.cue_table.setRowCount(0)
        self.main_waveform.clear_track()
        self._active_playback_cue_id = None
        self._last_playhead_position = -1.0
        self.set_active_cue_type(self.active_cue_type, apply_to_selected=False)
        self.sync_cue_time_editor_from_selection()
        self.now_playing.setText("No track selected")
        self.update_main_offset_ui()
        self.set_status("PLAYLIST CLEARED")

        self.update_playlist_overall_progress()

    # V1.2.5 project persistence
    # ---------------------------
    # Cue data lives only in self.cues_by_track and is serialized into the
    # .Tracyy project. These compatibility methods intentionally perform no
    # automatic disk I/O; CSV is created only by the explicit Export Cue action.
    def save_cue_storage(
        self,
        track_path: str | None = None,
    ) -> None:
        if hasattr(self, "mark_project_dirty"):
            self.mark_project_dirty()

    def save_cue_csv(
        self,
        track_path: str | None = None,
    ) -> None:
        if hasattr(self, "mark_project_dirty"):
            self.mark_project_dirty()

    def save_all_cue_csvs(self) -> None:
        if hasattr(self, "mark_project_dirty"):
            self.mark_project_dirty()

    def load_cue_csv_if_available(self, track_path: str) -> None:
        # Existing method name is retained so playlist/LTC call sites stay
        # stable. External save/*.json and csv/*.csv are no longer data sources.
        self.cues_by_track.setdefault(track_path, [])

    def on_waveform_marker_moved(
        self,
        cue_id: int,
        normalized_position: float,
    ) -> None:
        if not self.require_cue_edit_rights():
            return
        self._complete_pending_cue_table_now()
        cue = self.find_cue(cue_id)
        if cue is None or self.transport.duration <= 0:
            return

        self.push_cue_undo("MOVE CUE")
        new_seconds = max(
            0.0,
            min(
                self.transport.duration,
                normalized_position * self.transport.duration,
            ),
        )
        self._move_cue_to_time(
            cue_key(cue_id),
            new_seconds,
            select_after=True,
        )
        # marker_move_finished means the waveform already displayed the drag;
        # refresh once at release to canonicalize sort/order and label metadata.
        self._refresh_waveform_markers_direct()
        self.save_cue_csv()

    def main_track_path(self) -> str | None:
        if self.input_timecode_chase_enabled():
            path = str(self._input_timecode_active_path or "").strip()
            return path or None

        path = str(self.transport.current_file or "").strip()
        return path or None

    def current_track_cues(self) -> list[dict[str, object]]:
        path = self.main_track_path()
        if not path:
            return []
        return self.cues_by_track.setdefault(path, [])

    def markers_for_track(
        self,
        path: str,
        duration: float,
    ) -> list[
        tuple[
            int,
            float,
            str,
            str,
            bool,
            str,
        ]
    ]:
        if not path or duration <= 0:
            return []

        cues = [
            cue
            for cue in self.cues_by_track.get(
                path,
                [],
            )
            if self.cue_type_is_visible(
                str(
                    cue.get(
                        "type",
                        "Choreo",
                    )
                )
            )
        ]

        markers = []
        for cue in cues:
            cue_type = str(
                cue.get(
                    "type",
                    "Choreo",
                )
            )
            markers.append(
                (
                    cue_key(cue["id"]),
                    max(
                        0.0,
                        min(
                            1.0,
                            float(
                                cue.get(
                                    "seconds",
                                    0.0,
                                )
                            )
                            / duration,
                        ),
                    ),
                    str(cue.get("label") or cue.get("name") or ""),
                    str(cue.get("color") or self.cue_type_color(cue_type)),
                    self.cue_type_is_locked(cue_type),
                    self.cue_type_marker_shape(cue_type),
                )
            )

        return markers

    def refresh_waveform_markers(self) -> None:
        if self._updating_cue_table:
            return

        path = self.main_track_path()
        duration = self.transport.duration

        if not path or duration <= 0:
            self.main_waveform.set_markers([])
            return

        self.main_waveform.set_markers(self.markers_for_track(path, duration))

    def populate_cue_table_row(
        self,
        row: int,
        cue: dict[str, object],
        fps: int,
    ) -> None:
        cue_id = cue_key(cue["id"])
        cue_tc_seconds = self.media_to_timecode_seconds(float(cue["seconds"]))

        time_item = QTableWidgetItem(
            frames_to_timecode(
                round(cue_tc_seconds * fps),
                fps,
            )
        )

        cue_type = str(cue.get("type", "Choreo"))
        cue_color = str(cue.get("color") or self.cue_type_color(cue_type))

        type_item = QTableWidgetItem(cue_type)
        type_item.setForeground(QColor(cue_color))
        type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

        cue_item = QTableWidgetItem(str(cue["name"]))
        label_item = QTableWidgetItem(str(cue.get("label", "")))

        time_item.setFlags(time_item.flags() | Qt.ItemFlag.ItemIsEditable)
        cue_item.setFlags(cue_item.flags() | Qt.ItemFlag.ItemIsEditable)
        label_item.setFlags(label_item.flags() | Qt.ItemFlag.ItemIsEditable)

        for item in (
            time_item,
            type_item,
            cue_item,
            label_item,
        ):
            item.setData(Qt.ItemDataRole.UserRole, cue_id)

        self.cue_table.setItem(row, 0, time_item)
        self.cue_table.setItem(row, 1, type_item)
        self.cue_table.setItem(row, 2, cue_item)
        self.cue_table.setItem(row, 3, label_item)

    def _cue_insert_index(
        self,
        cues: list[dict[str, object]],
        seconds: float,
        cue_id: int,
    ) -> int:
        """Return stable chronological insertion row without sorting the list."""
        return bisect_right(
            [
                (
                    float(cue.get("seconds", 0.0)),
                    cue_key(cue.get("id")),
                )
                for cue in cues
            ],
            (float(seconds), cue_key(cue_id)),
        )

    def _refresh_waveform_markers_direct(self) -> None:
        """Refresh marker view even while the cue table is being batch-built."""
        path = self.main_track_path()
        duration = self.transport.duration
        if not path or duration <= 0:
            self.main_waveform.set_markers([])
            return
        self.main_waveform.set_markers(self.markers_for_track(path, duration))

    def _restore_cue_table_selection_after_batch(self) -> None:
        selected_ids = list(getattr(self, "_cue_table_pending_selected_ids", []))
        current_id = getattr(
            self,
            "_cue_table_pending_current_id",
            None,
        )
        if not selected_ids and current_id is None:
            return

        previous_block = self.cue_table.blockSignals(True)
        try:
            self.cue_table.clearSelection()
            for cue_id in selected_ids:
                row = self._cue_id_to_row.get(cue_key(cue_id))
                if row is not None and row < self.cue_table.rowCount():
                    self.cue_table.selectRow(row)
            if current_id is not None:
                row = self._cue_id_to_row.get(cue_key(current_id))
                if row is not None and row < self.cue_table.rowCount():
                    self.cue_table.setCurrentCell(row, 0)
        finally:
            self.cue_table.blockSignals(previous_block)

    def _complete_pending_cue_table_now(self) -> None:
        """Finish a background table build before a user edit mutates rows."""
        if not self._updating_cue_table:
            return
        cues = self._cue_table_pending_cues
        start = int(self._cue_table_pending_index)
        if not cues or start >= len(cues):
            return

        token = self._cue_table_load_token
        fps = int(self.fps.currentText())
        previous_block = self.cue_table.blockSignals(True)
        self.cue_table.setUpdatesEnabled(False)
        try:
            for row in range(start, len(cues)):
                self.populate_cue_table_row(row, cues[row], fps)
            self._cue_table_pending_index = len(cues)
        finally:
            self.cue_table.setUpdatesEnabled(True)
            self.cue_table.blockSignals(previous_block)
        self._finish_cue_table_batch(token)

    def _release_cue_table_batch(self) -> None:
        """Leave batch mode and rebuild the waveform markers.

        refresh_waveform_markers() does nothing while a batch is filling the
        table, so any refresh requested in that window is lost. A waveform
        preview arriving mid-batch calls set_track(), which clears the markers
        and then asks for exactly that dropped refresh. Rebuilding here is the
        one point that is guaranteed to run after batch mode ends.
        """
        self._cue_table_pending_cues = []
        self._cue_table_pending_index = 0
        self._cue_table_pending_path = None
        self._updating_cue_table = False
        self._refresh_waveform_markers_direct()

    def _finish_cue_table_batch(self, token: int) -> None:
        if token != self._cue_table_load_token:
            return
        self._release_cue_table_batch()
        self._restore_cue_table_selection_after_batch()
        self.cue_table.viewport().update()
        status_text = self.status_label.text() if hasattr(self, "status_label") else ""
        if str(status_text).startswith("CUE LOADING"):
            self.set_status(f"CUE READY — {self.cue_table.rowCount()} CUES")

    def continue_cue_table_batch(
        self,
        token: int,
    ) -> None:
        if token != self._cue_table_load_token:
            return

        pending_path = self._cue_table_pending_path
        if pending_path != self.main_track_path():
            # The track changed while the table was still filling. Batch mode
            # has to end here, otherwise the flag stays set for the rest of the
            # session and every later marker refresh is silently dropped.
            self._release_cue_table_batch()
            return

        cues = self._cue_table_pending_cues
        start = int(self._cue_table_pending_index)
        if start >= len(cues):
            self._finish_cue_table_batch(token)
            return

        batch_size = max(1, int(self._cue_table_batch_size))
        stop = min(len(cues), start + batch_size)
        fps = int(self.fps.currentText())

        previous_block = self.cue_table.blockSignals(True)
        self.cue_table.setUpdatesEnabled(False)
        try:
            for row in range(start, stop):
                self.populate_cue_table_row(row, cues[row], fps)
        finally:
            self.cue_table.setUpdatesEnabled(True)
            self.cue_table.blockSignals(previous_block)

        self._cue_table_pending_index = stop
        self.cue_table.viewport().update()

        if stop >= len(cues):
            self._finish_cue_table_batch(token)
            return

        QTimer.singleShot(
            max(0, int(self._cue_table_batch_interval_ms)),
            lambda active_token=token: self.continue_cue_table_batch(active_token),
        )

    def rebuild_cue_runtime_index(
        self,
        cues: list[dict[str, object]],
        reset_playback_state: bool = True,
    ) -> None:
        self._cue_sorted_cache = list(cues)
        self._cue_times_cache = [float(cue.get("seconds", 0.0)) for cue in cues]
        self._cue_id_to_row = {cue_key(cue.get("id")): row for row, cue in enumerate(cues)}
        self._cue_id_to_cue = {cue_key(cue.get("id")): cue for cue in cues}
        self._next_cue_index = bisect_right(
            self._cue_times_cache,
            (self.transport.current_position() + 0.001),
        )
        self._cue_runtime_rows.clear()
        if reset_playback_state:
            self._active_playback_cue_id = None
            self._last_playhead_position = -1.0
        elif (
            self._active_playback_cue_id is not None
            and cue_key(self._active_playback_cue_id) not in self._cue_id_to_cue
        ):
            self._active_playback_cue_id = None

        self._cue_manifest_revision += 1
        self._timeline_loading_window_key = None
        self._server_manifest_cache_key = None
        self._server_catalog_cache_key = None

    def reposition_cue_runtime_index(
        self,
        position_seconds: float,
    ) -> int:
        self._next_cue_index = bisect_right(
            self._cue_times_cache,
            float(position_seconds) + 0.001,
        )
        return self._next_cue_index

    def advance_cue_runtime_index(
        self,
        position_seconds: float,
    ) -> int:
        position_seconds = float(position_seconds)
        moved_backward = (
            self._last_playhead_position >= 0.0
            and position_seconds + 0.001 < self._last_playhead_position
        )
        large_jump = (
            self._last_playhead_position >= 0.0
            and abs(position_seconds - self._last_playhead_position) > 1.0
        )

        if moved_backward or large_jump:
            self.reposition_cue_runtime_index(position_seconds)
        else:
            cue_count = len(self._cue_times_cache)
            while (
                self._next_cue_index < cue_count
                and self._cue_times_cache[self._next_cue_index] <= position_seconds + 0.001
            ):
                self._next_cue_index += 1

        self._last_playhead_position = position_seconds
        return self._next_cue_index

    def cue_runtime_cues(
        self,
    ) -> list[dict[str, object]]:
        return self._cue_sorted_cache

    def _update_single_cue_row(self, cue_id: int) -> int | None:
        cue = self.find_cue(cue_id)
        row = self._cue_id_to_row.get(cue_key(cue_id))
        if cue is None or row is None:
            return None
        if row < 0 or row >= self.cue_table.rowCount():
            return None

        previous_block = self.cue_table.blockSignals(True)
        previous_updating = self._updating_cue_table
        self._updating_cue_table = True
        try:
            self.populate_cue_table_row(
                row,
                cue,
                int(self.fps.currentText()),
            )
        finally:
            self._updating_cue_table = previous_updating
            self.cue_table.blockSignals(previous_block)
        self.cue_table.viewport().update()
        return row

    def _insert_single_cue_row(
        self,
        row: int,
        cue: dict[str, object],
    ) -> None:
        previous_block = self.cue_table.blockSignals(True)
        previous_updating = self._updating_cue_table
        self._updating_cue_table = True
        try:
            self.cue_table.insertRow(int(row))
            self.populate_cue_table_row(
                int(row),
                cue,
                int(self.fps.currentText()),
            )
        finally:
            self._updating_cue_table = previous_updating
            self.cue_table.blockSignals(previous_block)
        self.cue_table.viewport().update()

    def _move_cue_to_time(
        self,
        cue_id: int,
        new_seconds: float,
        select_after: bool = True,
    ) -> int | None:
        cues = self.current_track_cues()
        old_row = self._cue_id_to_row.get(cue_key(cue_id))
        cue = self.find_cue(cue_id)
        if cue is None:
            return None

        if not self.commit_cue_operation(
            "move_cue",
            cue_key(cue_id),
            {"seconds": float(new_seconds)},
        ):
            self._update_single_cue_row(cue_key(cue_id))
            return None

        if old_row is None or old_row >= len(cues):
            for index, candidate in enumerate(cues):
                if cue_key(candidate.get("id")) == cue_key(cue_id):
                    old_row = index
                    break
        if old_row is None:
            return None

        cue = cues.pop(int(old_row))
        cue["seconds"] = float(new_seconds)
        new_row = self._cue_insert_index(
            cues,
            float(new_seconds),
            cue_key(cue_id),
        )
        cues.insert(new_row, cue)

        previous_block = self.cue_table.blockSignals(True)
        previous_updating = self._updating_cue_table
        self._updating_cue_table = True
        try:
            if int(old_row) < self.cue_table.rowCount():
                self.cue_table.removeRow(int(old_row))
            self.cue_table.insertRow(int(new_row))
            self.populate_cue_table_row(
                int(new_row),
                cue,
                int(self.fps.currentText()),
            )
        finally:
            self._updating_cue_table = previous_updating
            self.cue_table.blockSignals(previous_block)

        self.rebuild_cue_runtime_index(
            cues,
            reset_playback_state=False,
        )
        if select_after:
            self.cue_table.setCurrentCell(int(new_row), 0)
            self.cue_table.selectRow(int(new_row))
        self.cue_table.viewport().update()
        return int(new_row)

    def refresh_cue_table(self) -> None:
        """Full rebuild for track/project/FPS/bulk changes only.

        Normal add/edit/delete/marker operations use incremental row helpers and
        never clear/recreate the whole QTableWidget.
        """
        self._cue_table_load_token += 1
        token = self._cue_table_load_token

        selected_ids = self.selected_cue_ids()
        current_id = self.selected_cue_id()
        self._cue_table_pending_selected_ids = selected_ids
        self._cue_table_pending_current_id = current_id

        cues = self.current_track_cues()
        # A full refresh is a load/bulk boundary, so canonicalize the model once.
        cues.sort(
            key=lambda cue: (
                float(cue.get("seconds", 0.0)),
                cue_key(cue.get("id")),
            )
        )
        self.rebuild_cue_runtime_index(cues)

        self._cue_table_pending_cues = list(cues)
        self._cue_table_pending_index = 0
        self._cue_table_pending_path = self.main_track_path()
        self._updating_cue_table = True

        previous_block = self.cue_table.blockSignals(True)
        self.cue_table.setUpdatesEnabled(False)
        try:
            self.cue_table.clearContents()
            self.cue_table.setRowCount(len(cues))

            initial = min(
                len(cues),
                max(1, int(self._cue_table_initial_batch_size)),
            )
            fps = int(self.fps.currentText())
            for row in range(initial):
                self.populate_cue_table_row(row, cues[row], fps)
            self._cue_table_pending_index = initial
        finally:
            self.cue_table.setUpdatesEnabled(True)
            self.cue_table.blockSignals(previous_block)

        self._refresh_waveform_markers_direct()
        self.cue_table.viewport().update()

        if self._cue_table_pending_index >= len(cues):
            self._finish_cue_table_batch(token)
            return

        self.set_status(f"CUE LOADING — {self._cue_table_pending_index}/{len(cues)}")
        QTimer.singleShot(
            max(0, int(self._cue_table_batch_interval_ms)),
            lambda active_token=token: self.continue_cue_table_batch(active_token),
        )

    #: Records for songs this machine does not have. Media sync is what makes
    #: a track appear; the cues committed for it before that has to still be
    #: there when it does. Bounded because a client can sit in a session for
    #: hours without ever downloading a track it does not need.
    MAX_DEFERRED_CUE_OPS = 4000

    def deferred_cue_ops(self) -> list[dict]:
        ops = getattr(self, "_deferred_cue_ops", None)
        if ops is None:
            ops = []
            self._deferred_cue_ops = ops
        return ops

    def cue_document_for_session(self) -> dict[str, list[dict]]:
        """This machine's cues, keyed the way the session names tracks."""
        document: dict[str, list[dict]] = {}
        for path, cues in self.cues_by_track.items():
            if not cues:
                continue
            track_id = self.track_id_for_path(str(path))
            if track_id:
                document[track_id] = [dict(cue) for cue in cues]
        return document

    def apply_cue_snapshot(self, snapshot: dict) -> int:
        """Take the host's whole cue document as this machine's own.

        For a machine that just joined. The journal only carries what changed
        during the session, and most of a show existed before anybody
        connected — replaying operations would leave a laptop that arrived at
        the interval with an almost empty cue sheet.

        The host's copy wins outright for every track it mentions. That is
        what joining somebody else's session means, and pretending otherwise
        would be a silent merge nobody asked for.
        """
        tracks = snapshot.get("tracks")
        if not isinstance(tracks, dict):
            return 0

        touched: set[str] = set()
        applied = 0
        for track_id, cues in tracks.items():
            if not isinstance(cues, list):
                continue
            path = self.path_for_track_id(str(track_id))
            if not path:
                # The song is not here yet. Kept as ordinary additions so the
                # same path that handles a late download handles this too.
                for cue in cues:
                    self.deferred_cue_ops().append(
                        {
                            "track_id": str(track_id),
                            "cue_id": str(cue.get("id") or ""),
                            "kind": "add_cue",
                            "payload": dict(cue),
                        }
                    )
                del self.deferred_cue_ops()[: -self.MAX_DEFERRED_CUE_OPS]
                continue

            rebuilt = []
            for cue in cues:
                cue_id = cue_key(cue.get("id"))
                if not cue_id:
                    continue
                cue_type = str(cue.get("type") or "Choreo")
                rebuilt.append(
                    {
                        "id": cue_id,
                        "seconds": float(cue.get("seconds") or 0.0),
                        "name": str(cue.get("name") or ""),
                        "label": str(cue.get("label") or ""),
                        "type": cue_type,
                        "color": str(cue.get("color") or "") or self.cue_type_color(cue_type),
                    }
                )
            rebuilt.sort(key=lambda cue: float(cue.get("seconds") or 0.0))
            self.cues_by_track[path] = rebuilt
            touched.add(path)
            applied += len(rebuilt)

        for path in touched:
            self.save_cue_csv(path)
        if touched:
            self.refresh_after_remote_cue_ops(str(self.main_track_path() or "") in touched)
        return applied

    def apply_remote_cue_ops(self, records: list[dict]) -> int:
        """Apply operations another machine committed. Returns how many landed.

        The one door for changes that did not start here, and the reason
        :class:`~tracyy.sync.document.MutationOrigin` exists: nothing on this
        path may submit an operation back, or two machines would bounce the
        same edit between them for as long as the session lasts.

        Every branch tolerates being handed the same record twice. The host
        repeats anything unacknowledged on every beat, so a reply lost on the
        way back is not an error condition — it is Tuesday.
        """
        touched: set[str] = set()
        applied = 0

        for record in records:
            track_id = str(record.get("track_id") or "")
            cue_id = cue_key(record.get("cue_id"))
            kind = str(record.get("kind") or "")
            payload = record.get("payload") or {}
            if not track_id or not cue_id:
                continue

            path = self.path_for_track_id(track_id)
            if not path:
                self.deferred_cue_ops().append(dict(record))
                del self.deferred_cue_ops()[: -self.MAX_DEFERRED_CUE_OPS]
                continue

            cues = self.cues_by_track.setdefault(path, [])
            index = next(
                (
                    position
                    for position, cue in enumerate(cues)
                    if cue_key(cue.get("id")) == cue_id
                ),
                None,
            )

            if kind == "add_cue":
                if index is not None:
                    # Already here. A resend, not a second cue.
                    continue
                cue_type = str(payload.get("type") or "Choreo")
                cues.append(
                    {
                        "id": cue_id,
                        "seconds": float(payload.get("seconds") or 0.0),
                        "name": str(payload.get("name") or ""),
                        "label": str(payload.get("label") or ""),
                        "type": cue_type,
                        "color": str(payload.get("color") or "")
                        or self.cue_type_color(cue_type),
                    }
                )
            elif index is None:
                # A change to a cue this machine never had. Not an error: the
                # add for it is sitting in the deferred pile, or arrived
                # before this machine joined.
                continue
            elif kind == "delete_cue":
                cues.pop(index)
            elif kind == "move_cue":
                cues[index]["seconds"] = float(payload.get("seconds") or 0.0)
            elif kind == "update_cue_fields":
                for field in ("name", "label", "type", "color"):
                    if field in payload:
                        cues[index][field] = str(payload[field])
            else:
                continue

            cues.sort(key=lambda cue: float(cue.get("seconds") or 0.0))
            touched.add(path)
            applied += 1

        for path in touched:
            self.save_cue_csv(path)
        if touched:
            self.refresh_after_remote_cue_ops(str(self.main_track_path() or "") in touched)
        return applied

    def refresh_after_remote_cue_ops(self, open_track_changed: bool) -> None:
        """Redraw once for a whole batch, and never over somebody's typing."""
        if not open_track_changed:
            return
        if getattr(self, "_cue_table_label_editor_active", False):
            # Rebuilding the table closes the open editor and throws away what
            # is half typed in it. The data is already updated; the rows catch
            # up the moment the editor closes.
            self._cue_table_remote_refresh_pending = True
            return
        self._cue_table_remote_refresh_pending = False
        self.refresh_cue_table()
        self._refresh_waveform_markers_direct()

    def add_cue(self) -> None:
        if not self.require_cue_edit_rights():
            return
        self._complete_pending_cue_table_now()
        path = self.main_track_path()
        if not path:
            self.show_error("Hãy chọn một bài nhạc trước.")
            return

        cues = self.current_track_cues()
        # Minted from this machine's origin, not from max(id) + 1 on the
        # track: that counter is local, so two people adding a cue to the same
        # song both produced the same number and the two machines silently
        # meant different cues by one name.
        next_id = self.mint_cue_id()
        new_cue = {
            "id": next_id,
            "seconds": self.transport.current_position(),
            "name": f"Cue {len(cues) + 1}",
            "label": "",
            "type": self.active_cue_type,
            "color": self.cue_type_color(self.active_cue_type),
        }
        if not self.commit_cue_operation(
            "add_cue",
            cue_key(next_id),
            {
                "seconds": float(new_cue["seconds"]),
                "name": str(new_cue["name"]),
                "label": "",
                "type": str(new_cue["type"]),
                "color": str(new_cue["color"]),
            },
        ):
            return

        self.push_cue_undo("ADD CUE")

        insert_row = self._cue_insert_index(
            cues,
            float(new_cue["seconds"]),
            cue_key(next_id),
        )
        cues.insert(insert_row, new_cue)
        self._insert_single_cue_row(insert_row, new_cue)
        self.rebuild_cue_runtime_index(
            cues,
            reset_playback_state=False,
        )
        self._refresh_waveform_markers_direct()
        self.save_cue_csv()

        self.cue_table.setCurrentCell(insert_row, 0)
        self.cue_table.selectRow(insert_row)
        item = self.cue_table.item(insert_row, 0)
        if item is not None:
            self.cue_table.scrollToItem(item)
        self.sync_cue_time_editor_from_selection()

    def selected_cue_ids(self) -> list[int]:
        selection_model = self.cue_table.selectionModel()
        if selection_model is None:
            return []

        selected_ids: list[int] = []
        seen: set[int] = set()

        for model_index in selection_model.selectedRows(0):
            item = self.cue_table.item(model_index.row(), 0)
            if item is None:
                continue

            value = item.data(Qt.ItemDataRole.UserRole)
            if value is None:
                continue

            cue_id = cue_key(value)
            if not cue_id:
                continue

            if cue_id in seen:
                continue

            seen.add(cue_id)
            selected_ids.append(cue_id)

        return selected_ids

    def selected_cue_id(self) -> int | None:
        selected_ids = self.selected_cue_ids()
        if selected_ids:
            current_row = self.cue_table.currentRow()
            if current_row >= 0:
                current_item = self.cue_table.item(current_row, 0)
                if current_item is not None:
                    value = current_item.data(Qt.ItemDataRole.UserRole)
                    current_id = cue_key(value) or None
                    if current_id in selected_ids:
                        return current_id

            return selected_ids[0]

        row = self.cue_table.currentRow()
        if row < 0:
            return None

        item = self.cue_table.item(row, 0)
        if item is None:
            return None

        value = item.data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    def find_cue(self, cue_id: str) -> dict[str, object] | None:
        # O(1) lookup during playback/selection instead of scanning every cue.
        cached = self._cue_id_to_cue.get(cue_key(cue_id))
        if cached is not None:
            return cached

        # Fallback is only for transient edit/load states before the cache
        # has been rebuilt.
        for cue in self.current_track_cues():
            if cue_key(cue["id"]) == cue_key(cue_id):
                return cue
        return None

    def go_to_selected_cue(self) -> None:
        cue_id = self.selected_cue_id()
        if cue_id is None:
            return
        cue = self.find_cue(cue_id)
        if cue is not None:
            self.transport.seek(float(cue["seconds"]))

    def delete_selected_cue(self) -> None:
        if not self.require_cue_edit_rights():
            return
        self._complete_pending_cue_table_now()
        selected_ids = set(self.selected_cue_ids())
        if not selected_ids:
            cue_id = self.selected_cue_id()
            if cue_id is None:
                return
            selected_ids = {cue_id}

        self.push_cue_undo(
            f"DELETE {len(selected_ids)} CUE" + ("S" if len(selected_ids) != 1 else "")
        )

        selected_ids = [
            cue_id
            for cue_id in selected_ids
            if self.commit_cue_operation("delete_cue", cue_id, {})
        ]
        if not selected_ids:
            return

        cues = self.current_track_cues()
        rows_to_remove = sorted(
            (
                self._cue_id_to_row[cue_id]
                for cue_id in selected_ids
                if cue_id in self._cue_id_to_row
            ),
            reverse=True,
        )
        original_count = len(cues)
        cues[:] = [cue for cue in cues if cue_key(cue["id"]) not in selected_ids]
        deleted_count = original_count - len(cues)
        if deleted_count <= 0:
            return

        previous_block = self.cue_table.blockSignals(True)
        previous_updating = self._updating_cue_table
        self._updating_cue_table = True
        self.cue_table.setUpdatesEnabled(False)
        try:
            for row in rows_to_remove:
                if 0 <= row < self.cue_table.rowCount():
                    self.cue_table.removeRow(row)
        finally:
            self.cue_table.setUpdatesEnabled(True)
            self._updating_cue_table = previous_updating
            self.cue_table.blockSignals(previous_block)

        self.rebuild_cue_runtime_index(
            cues,
            reset_playback_state=False,
        )
        self._refresh_waveform_markers_direct()
        self.cue_table.viewport().update()
        self.save_cue_csv()
        self.sync_cue_time_editor_from_selection()
        self.set_status(f"DELETED {deleted_count} CUE" + ("S" if deleted_count != 1 else ""))

    def cue_id_from_table_item(
        self,
        item: QTableWidgetItem | None,
    ) -> str | None:
        """The cue a table item belongs to, or None when it carries no id.

        Ids stopped being integers in project version 4: they are opaque
        strings minted per machine, so two people adding a cue at the same
        moment cannot both mint the same one. This used to coerce with
        ``int()``, which turned every id minted since — anything of the form
        ``4ce550-1`` — into ``None`` and quietly disabled the click and
        double-click handlers that ask for it.
        """
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        if value is None:
            return None
        return cue_key(value) or None

    def seek_to_cue_id(self, cue_id: str) -> None:
        cue = self.find_cue(cue_id)
        if cue is None:
            return

        seconds = max(
            0.0,
            min(
                self.transport.duration,
                float(cue.get("seconds", 0.0)),
            ),
        )
        self.transport.seek(seconds)
        self.refresh_position()
        self.select_cue_row_by_id(cue_id)

    def on_cue_table_item_clicked(
        self,
        item: QTableWidgetItem,
    ) -> None:
        if self._updating_cue_table:
            return

        cue_id = self.cue_id_from_table_item(item)
        if cue_id is None:
            return

        column = item.column()

        # TIME or CUE TYPE: seek immediately.
        if column in (0, 1):
            self.seek_to_cue_id(cue_id)
            return

        # CUE or LABEL: rename with one click. LABEL additionally owns the
        # table selection while the mouse/editor is on it.
        if column in (2, 3):
            if column == 3:
                self.set_cue_table_label_hover(True)
            self.cue_table.setCurrentItem(item)
            QTimer.singleShot(
                0,
                lambda target=item: self.cue_table.editItem(target),
            )

    def on_cue_table_item_double_clicked(
        self,
        item: QTableWidgetItem,
    ) -> None:
        if self._updating_cue_table:
            return

        cue_id = self.cue_id_from_table_item(item)
        if cue_id is None:
            return

        # TIME: seek first, then edit marker time.
        if item.column() == 0:
            self.seek_to_cue_id(cue_id)
            self.cue_table.setCurrentItem(item)
            QTimer.singleShot(
                0,
                lambda target=item: self.cue_table.editItem(target),
            )
            return

        # CUE / LABEL keep editing.
        if item.column() in (2, 3):
            self.cue_table.setCurrentItem(item)
            QTimer.singleShot(
                0,
                lambda target=item: self.cue_table.editItem(target),
            )

    def on_cue_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_cue_table:
            return
        if not self.require_cue_edit_rights():
            cue_id = item.data(Qt.ItemDataRole.UserRole)
            if cue_id is not None:
                self._update_single_cue_row(cue_key(cue_id))
            return

        cue_id_value = item.data(Qt.ItemDataRole.UserRole)
        if cue_id_value is None:
            return

        cue_id = cue_key(cue_id_value)
        cue = self.find_cue(cue_id)
        if cue is None:
            return

        self.push_cue_undo("EDIT CUE")
        column = item.column()

        try:
            if column == 0:
                fps = int(self.fps.currentText())
                frames = timecode_to_frames(
                    item.text(),
                    fps,
                )
                absolute_seconds = frames / fps
                media_seconds = self.timecode_to_media_seconds(absolute_seconds)
                new_seconds = max(
                    0.0,
                    min(self.transport.duration, media_seconds),
                )
                self._move_cue_to_time(
                    cue_id,
                    new_seconds,
                    select_after=True,
                )
            elif column == 2:
                name = item.text().strip() or f"Cue {item.row() + 1}"
                if not self.commit_cue_operation(
                    "update_cue_fields", cue_id, {"name": name}
                ):
                    self._update_single_cue_row(cue_id)
                    return
                cue["name"] = name
                self.rebuild_cue_runtime_index(
                    self.current_track_cues(),
                    reset_playback_state=False,
                )
                self._update_single_cue_row(cue_id)
            elif column == 3:
                label = item.text().strip()
                if not self.commit_cue_operation(
                    "update_cue_fields", cue_id, {"label": label}
                ):
                    self._update_single_cue_row(cue_id)
                    return
                cue["label"] = label
                self.rebuild_cue_runtime_index(
                    self.current_track_cues(),
                    reset_playback_state=False,
                )
                self._update_single_cue_row(cue_id)
            else:
                return
        except Exception as exc:
            self.show_error(str(exc))
            self._update_single_cue_row(cue_id)
            return

        self._refresh_waveform_markers_direct()
        self.save_cue_csv()
