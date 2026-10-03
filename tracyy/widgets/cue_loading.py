from __future__ import annotations

import time

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from tracyy.core.ids import cue_key


class VirtualCueLoadingWidget(QWidget):
    """Virtual cue loading list painted on one canvas."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._entries: list[dict[str, object]] = []
        self._target_id: int | None = None
        self._scroll_offset = 0.0
        self._desired_offset = 0.0
        self._row_height = 44.0
        self._spacing = 4.0
        self._countdown_window = 5.0
        self._manual_hold_until = 0.0
        self._anchor_position = 0.0
        self._anchor_monotonic = time.perf_counter()
        self._anchor_running = False

        #: Height covered by the transport bar laid over this widget. Rows
        #: still paint the full height — sliding under the bar is the point —
        #: but anything centred has to centre in what is actually visible.
        self._bottom_inset = 0.0

        self.setMinimumHeight(120)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

    def set_bottom_inset(self, inset: float) -> None:
        inset = max(0.0, float(inset))
        if abs(inset - self._bottom_inset) < 0.5:
            return
        self._bottom_inset = inset
        self._recalculate_row_height()
        self.update()

    def visible_rect(self):
        rect = self.rect()
        rect.setHeight(max(60, rect.height() - round(self._bottom_inset)))
        return rect

    def set_entries(
        self,
        entries: list[dict[str, object]],
        target_cue_id: int | None,
        countdown_window: float,
    ) -> None:
        self._entries = entries
        self._target_id = target_cue_id
        self._countdown_window = max(
            0.1,
            float(countdown_window),
        )

        if not entries:
            self._scroll_offset = 0.0
            self._desired_offset = 0.0
            self.update()
            return

        target_index = 0
        if target_cue_id is not None:
            for index, entry in enumerate(entries):
                if cue_key(entry["id"]) == cue_key(target_cue_id):
                    target_index = index
                    break
            else:
                target_index = max(
                    0,
                    len(entries) - 1,
                )

        self._recalculate_row_height()
        stride = max(
            1.0,
            self._row_height + self._spacing,
        )
        anchor_rows = max(
            0.0,
            self.height() * 0.28 / stride,
        )
        self._desired_offset = max(
            0.0,
            min(
                self.maximum_offset(),
                target_index - anchor_rows,
            ),
        )

        if abs(self._desired_offset - self._scroll_offset) > max(
            12.0,
            self.visible_row_count() * 2.0,
        ):
            self._scroll_offset = self._desired_offset

        self._animate_offset_step()
        self.update()

    def estimated_position(self) -> float:
        if not self._anchor_running:
            return max(
                0.0,
                self._anchor_position,
            )

        elapsed = max(
            0.0,
            time.perf_counter() - self._anchor_monotonic,
        )
        return max(
            0.0,
            self._anchor_position + elapsed,
        )

    def set_transport_anchor(
        self,
        position: float,
        running: bool,
    ) -> None:
        position = max(
            0.0,
            float(position),
        )
        running = bool(running)
        estimated = self.estimated_position()
        state_changed = running != self._anchor_running
        drift = position - estimated
        timeline_jump = abs(drift) > 0.12

        if state_changed or timeline_jump or not running:
            self._anchor_position = position
            self._anchor_monotonic = time.perf_counter()
        elif abs(drift) > 0.025:
            self._anchor_position = estimated + drift * 0.20
            self._anchor_monotonic = time.perf_counter()

        self._anchor_running = running
        self.update()

    def _recalculate_row_height(self) -> None:
        # Measured against the visible area, not the widget: the transport bar
        # covers the bottom of this widget, and sizing rows for space nobody
        # can see pushed them to the 72px ceiling the moment the list was given
        # full height. More rows, shorter, is also what the list is for — an
        # operator wants to see what is coming, not four large cards.
        viewport_height = max(120.0, float(self.height()) - self._bottom_inset)
        ideal_rows = 7.5
        height = (viewport_height - 8.0 - self._spacing * (ideal_rows - 1.0)) / ideal_rows
        self._row_height = max(32.0, min(56.0, height))

    def visible_row_count(self) -> int:
        stride = max(1.0, self._row_height + self._spacing)
        return max(1, int(self.height() / stride) + 2)

    def maximum_offset(self) -> float:
        visible = max(
            1.0,
            self.height() / max(1.0, self._row_height + self._spacing),
        )
        return max(0.0, len(self._entries) - visible)

    def _animate_offset_step(self) -> None:
        if time.perf_counter() < self._manual_hold_until:
            return

        delta = self._desired_offset - self._scroll_offset
        if abs(delta) < 0.002:
            self._scroll_offset = self._desired_offset
            return

        self._scroll_offset += delta * 0.16
        self._scroll_offset = max(
            0.0,
            min(self.maximum_offset(), self._scroll_offset),
        )

    def wheelEvent(self, event) -> None:
        if not self._entries:
            event.ignore()
            return

        steps = event.angleDelta().y() / 120.0
        self._scroll_offset = max(
            0.0,
            min(
                self.maximum_offset(),
                self._scroll_offset - steps * 1.8,
            ),
        )
        self._desired_offset = self._scroll_offset
        self._manual_hold_until = time.perf_counter() + 1.4
        self.update()
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._recalculate_row_height()
        self._desired_offset = min(
            self._desired_offset,
            self.maximum_offset(),
        )
        self._scroll_offset = min(
            self._scroll_offset,
            self.maximum_offset(),
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#111019"))
        current_position = self.estimated_position()

        if not self._entries:
            painter.setPen(QColor("#8c8995"))
            painter.drawText(
                self.visible_rect(),
                int(Qt.AlignmentFlag.AlignCenter),
                "No upcoming cues",
            )
            return

        self._animate_offset_step()
        if abs(self._desired_offset - self._scroll_offset) > 0.002:
            QTimer.singleShot(16, self.update)

        stride = self._row_height + self._spacing
        first_index = max(0, int(self._scroll_offset))
        fractional = self._scroll_offset - first_index
        y = 4.0 - fractional * stride
        last_index = min(
            len(self._entries),
            first_index + self.visible_row_count(),
        )

        for index in range(first_index, last_index):
            entry = self._entries[index]
            row_rect = QRectF(
                4.0,
                y,
                max(0.0, self.width() - 8.0),
                self._row_height,
            )
            y += stride

            if row_rect.bottom() < 0:
                continue
            if row_rect.top() > self.height():
                break

            color = QColor(str(entry.get("color", "#4568d8")))
            if not color.isValid():
                color = QColor("#4568d8")

            remaining = float(entry.get("seconds", 0.0)) - current_position
            if 0.0 < remaining <= self._countdown_window:
                progress = max(
                    0.0,
                    min(
                        1.0,
                        (self._countdown_window - remaining) / self._countdown_window,
                    ),
                )
            elif remaining <= 0.0:
                progress = 1.0
            else:
                progress = 0.0

            base_color = QColor(color)
            base_color.setAlpha(90)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(base_color)
            painter.drawRoundedRect(row_rect, 9.0, 9.0)

            if progress > 0.0:
                fill_rect = QRectF(row_rect)
                fill_rect.setWidth(row_rect.width() * progress)
                fill_color = QColor(color)
                fill_color.setAlpha(220)
                painter.setBrush(fill_color)
                painter.drawRoundedRect(
                    fill_rect,
                    9.0,
                    9.0,
                )

            if self._target_id is not None and cue_key(entry["id"]) == cue_key(
                self._target_id
            ):
                painter.setPen(QPen(QColor(color).lighter(150), 1.6))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(
                    row_rect.adjusted(
                        0.8,
                        0.8,
                        -0.8,
                        -0.8,
                    ),
                    9.0,
                    9.0,
                )

            cue_type = str(entry.get("type", ""))
            cue_name = str(entry.get("label") or entry.get("name") or "Cue")
            title = f"{cue_type}  ·  {cue_name}" if cue_type else cue_name

            font = QFont(self.font())
            font.setPointSizeF(
                max(
                    10.0,
                    min(14.0, self._row_height * 0.22),
                )
            )
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor("#ffffff"))

            countdown_width = 112.0
            title_rect = row_rect.adjusted(
                14.0,
                0.0,
                -countdown_width,
                0.0,
            )
            fitted_title = QFontMetrics(font).elidedText(
                title,
                Qt.TextElideMode.ElideRight,
                max(20, int(title_rect.width())),
            )
            painter.drawText(
                title_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                fitted_title,
            )

            if remaining <= 0.0:
                countdown = ""
            elif remaining >= 60.0:
                whole = round(remaining)
                minutes, seconds = divmod(whole, 60)
                countdown = f"-{minutes:02d}:{seconds:02d}"
            else:
                countdown = f"-{remaining:.1f}s"

            painter.setPen(QColor("#e8e8ec"))
            countdown_rect = row_rect.adjusted(
                max(
                    0.0,
                    row_rect.width() - countdown_width,
                ),
                0.0,
                -12.0,
                0.0,
            )
            painter.drawText(
                countdown_rect,
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                countdown,
            )

        if self._anchor_running:
            QTimer.singleShot(
                16,
                self.update,
            )
