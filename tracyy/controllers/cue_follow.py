from __future__ import annotations

from bisect import bisect_right

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QAbstractItemView

from tracyy.core.ids import cue_key
from tracyy.widgets import CUE_COLUMN_KEYS


class CueFollowControllerMixin:
    def active_cue_for_position(
        self,
        position_seconds: float,
    ) -> dict[str, object] | None:
        if not self._cue_sorted_cache:
            return None

        index = (
            bisect_right(
                self._cue_times_cache,
                position_seconds + 0.001,
            )
            - 1
        )

        if index < 0:
            return None

        return self._cue_sorted_cache[index]

    def cue_table_follow_locked(self) -> bool:
        return bool(
            self._cue_table_manual_browse
            or self._cue_table_label_hover
            or self._cue_table_label_editor_active
        )

    def resume_cue_table_follow_if_unlocked(self) -> None:
        if self.cue_table_follow_locked():
            return
        # Force the current playback cue to be applied again even when its ID
        # did not change while the user was hovering/editing LABEL.
        self._active_playback_cue_id = None
        timeline_position = self.current_timeline_media_position()
        self.update_cue_selection_from_playhead(timeline_position)

    #: The cell this machine has open in an editor, as
    #: ``(cue_id, column_key)``. Empty when nobody is typing. Read by the sync
    #: controller on the network tick and sent to the other machines.
    local_editing_cell: tuple[str, str] = ("", "")

    def on_cue_table_editor_started(
        self,
        row: int,
        column: int,
    ) -> None:
        # Recorded for every column, before the LABEL-only follow logic below:
        # the other machines want to know which cell is being typed in, and
        # that is just as true of TIME and CUE as it is of LABEL.
        item = self.cue_table.item(int(row), int(column))
        cue_id = self.cue_id_from_table_item(item)
        if cue_id and 0 <= int(column) < len(CUE_COLUMN_KEYS):
            self.local_editing_cell = (cue_id, CUE_COLUMN_KEYS[int(column)])

        if int(column) != 3:
            return
        self._cue_table_label_editor_active = True
        item = self.cue_table.item(int(row), int(column))
        self._cue_table_label_editor_cue_id = self.cue_id_from_table_item(item)

    def on_cue_table_editor_closed(self, *_args) -> None:
        self.local_editing_cell = ("", "")
        if getattr(self, "_cue_table_remote_refresh_pending", False):
            # Changes arrived from another machine while this one was typing.
            # They are in the data already; this is the redraw that was held
            # back so it could not eat the half-finished edit.
            self.refresh_after_remote_cue_ops(True)
        if not self._cue_table_label_editor_active:
            return
        self._cue_table_label_editor_active = False
        self._cue_table_label_editor_cue_id = None
        QTimer.singleShot(
            0,
            self.resume_cue_table_follow_if_unlocked,
        )

    def set_cue_table_label_hover(
        self,
        hovering: bool,
    ) -> None:
        hovering = bool(hovering)
        if hovering == self._cue_table_label_hover:
            return
        self._cue_table_label_hover = hovering
        if not hovering:
            QTimer.singleShot(
                0,
                self.resume_cue_table_follow_if_unlocked,
            )

    def select_cue_row_by_id(
        self,
        cue_id: int,
    ) -> None:
        row = self._cue_id_to_row.get(cue_key(cue_id))
        if row is None:
            return

        item = self.cue_table.item(row, 0)
        if item is None:
            return

        if self.cue_table_follow_locked():
            return

        selection_changed = self.cue_table.currentRow() != row
        if not selection_changed:
            return

        previous_block = self.cue_table.blockSignals(True)
        try:
            self.cue_table.setCurrentCell(
                row,
                0,
            )
            self.cue_table.selectRow(row)

            # Keep the active/highlighted cue at the first visible row.
            # This runs only when the active cue actually changes.
            self.cue_table.scrollToItem(
                item,
                QAbstractItemView.ScrollHint.PositionAtTop,
            )
        finally:
            self.cue_table.blockSignals(previous_block)

    def sorted_current_cues(
        self,
    ) -> list[dict[str, object]]:
        return self._cue_sorted_cache

    def cue_interval_for_position(
        self,
        position_seconds: float,
    ) -> tuple[
        dict[str, object] | None,
        dict[str, object] | None,
        float,
        float,
    ]:
        cues = self._cue_sorted_cache
        if not cues:
            return None, None, 0.0, 0.0

        next_index = bisect_right(
            self._cue_times_cache,
            float(position_seconds) + 0.0005,
        )
        previous_cue = cues[next_index - 1] if next_index > 0 else None
        next_cue = cues[next_index] if next_index < len(cues) else None
        interval_start = self._cue_times_cache[next_index - 1] if next_index > 0 else 0.0
        interval_end = (
            self._cue_times_cache[next_index] if next_index < len(cues) else interval_start
        )
        return previous_cue, next_cue, interval_start, interval_end

    def clear_inline_cue_loading(self) -> None:
        self._cue_runtime_rows.clear()

    def update_timeline_loading_geometry(self) -> None:
        if hasattr(self, "timeline_loading_view"):
            self.timeline_loading_view.update()

    def scroll_timeline_loading_to_cue(
        self,
        cue_id: int | None,
        animated: bool = True,
    ) -> None:
        return

    def update_timeline_loading_bars(
        self,
        cues: list[dict[str, object]],
        countdown_window: float,
        target_cue_id: int | None,
    ) -> None:
        entries: list[dict[str, object]] = []

        for cue in cues:
            cue_id = cue_key(cue.get("id"))
            cue_type = str(cue.get("type", "Choreo"))
            entries.append(
                {
                    "id": cue_id,
                    "name": str(cue.get("name", "")),
                    "label": str(cue.get("label") or cue.get("name") or "Cue"),
                    "type": cue_type,
                    "color": str(cue.get("color") or self.cue_type_color(cue_type)),
                    "seconds": float(
                        cue.get(
                            "seconds",
                            0.0,
                        )
                    ),
                }
            )

        self.timeline_loading_view.set_entries(
            entries,
            target_cue_id,
            countdown_window,
        )

    def update_next_cue_loading(
        self,
        position_seconds: float,
    ) -> None:
        if not self._cue_countdown_enabled:
            self.clear_inline_cue_loading()
            return

        self.timeline_loading_view.set_transport_anchor(
            position_seconds,
            self.transport.running,
        )

        countdown_window = 5.0
        cues = self._cue_sorted_cache
        times = self._cue_times_cache
        next_index = bisect_right(
            times,
            position_seconds + 0.0005,
        )

        visibility_key = tuple(
            (
                str(name),
                bool(
                    self.cue_type_meta.get(
                        name,
                        {},
                    ).get(
                        "visible",
                        True,
                    )
                ),
            )
            for name, _color in self.cue_type_defs
        )
        window_key = (
            self.main_track_path(),
            self._cue_manifest_revision,
            next_index,
            visibility_key,
        )

        if window_key == self._timeline_loading_window_key:
            return

        self._timeline_loading_window_key = window_key

        if not cues:
            self.timeline_loading_view.set_entries(
                [],
                None,
                countdown_window,
            )
            return

        visible_cues: list[dict[str, object]] = []
        target_cue_id: int | None = None

        for cue in cues[next_index:]:
            cue_type = str(cue.get("type", "Choreo"))
            if not self.cue_type_is_visible(cue_type):
                continue

            visible_cues.append(cue)
            if target_cue_id is None:
                target_cue_id = cue_key(cue.get("id"))

            if len(visible_cues) >= 40:
                break

        self.update_timeline_loading_bars(
            visible_cues,
            countdown_window,
            target_cue_id,
        )

    def update_cue_selection_from_playhead(
        self,
        position_seconds: float,
    ) -> None:
        if not self._cue_runtime_enabled:
            return

        if not self.current_playlist_path():
            return

        next_index = self.advance_cue_runtime_index(position_seconds)
        active = self._cue_sorted_cache[next_index - 1] if next_index > 0 else None
        active_id = cue_key(active["id"]) if active is not None else None

        # The common playback path exits here without touching QTableWidget.
        if active_id == self._active_playback_cue_id:
            return

        self._active_playback_cue_id = active_id

        if active_id is None:
            previous_block = self.cue_table.blockSignals(True)
            try:
                self.cue_table.clearSelection()
            finally:
                self.cue_table.blockSignals(previous_block)
            self.cue_table.setProperty("activeCueId", None)
            return

        self.select_cue_row_by_id(active_id)
        self.cue_table.setProperty("activeCueId", active_id)

        cue_name = str(active.get("label") or active.get("name") or f"Cue {active_id}")
        cue_type = str(active.get("type", ""))
        self.set_status(f"ACTIVE CUE — {cue_type}: {cue_name}")
