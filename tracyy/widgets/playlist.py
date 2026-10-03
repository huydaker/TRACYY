from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QDrag,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLineEdit,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
)

from .constants import (
    AUDIO_EXTENSIONS,
    PLAYLIST_MISSING_ROLE,
    PLAYLIST_TITLE_ROLE,
)

#: Row geometry, shared by the card painter and the offset editor.
#:
#: The two live in different columns but describe one card, so the numbers have
#: to come from one place — an editor that opens a few pixels off the text it
#: replaces is the kind of thing nobody reports and everybody notices.
CARD_ROW_HEIGHT = 58
CARD_MARGIN_Y = 4
CARD_RADIUS = 12
CARD_PAD_X = 14
CARD_NUMBER_WIDTH = 24
CARD_NUMBER_GAP = 10
CARD_TITLE_TOP = 9
CARD_LINE_GAP = 3
CARD_STATE_WIDTH = 32

#: Type sizes, as steps from the application font.
#:
#: Named rather than written at each call because the title's size is used in
#: two places — the painter and the rect maths the offset editor opens into —
#: and a number that disagrees between them puts the editor off the text.
CARD_TITLE_DELTA = 0.0
CARD_OFFSET_DELTA = -0.5
CARD_NUMBER_DELTA = 1.5
CARD_BADGE_DELTA = -2.0

#: Diameter of the state dot. Small enough to stop competing with the title,
#: large enough to read its colour at a glance.
CARD_DOT_SIZE = 8.0


def scaled_font(
    base: QFont,
    delta_points: float,
    weight: QFont.Weight | None = None,
) -> QFont:
    """``base`` resized by ``delta_points``, in whatever unit it actually uses.

    Tracyy sets its fonts from the stylesheet in pixels, and a font sized in
    pixels reports ``pointSizeF() == -1``. Subtracting from that produces a
    negative size, which Qt rejects and silently leaves at the default — every
    line of every card ending up the same size. Branch on which unit is set.
    """
    font = QFont(base)
    if weight is not None:
        font.setWeight(weight)

    points = base.pointSizeF()
    if points > 0:
        font.setPointSizeF(max(1.0, points + delta_points))
        return font

    pixels = base.pixelSize()
    if pixels <= 0:
        return font
    # Points are 4/3 of a pixel at Qt's nominal 96 dpi; rounding away from
    # zero keeps a half-point step from vanishing.
    step = delta_points * 4.0 / 3.0
    shift = int(step + (0.5 if step > 0 else -0.5))
    font.setPixelSize(max(1, pixels + shift))
    return font


def card_rect_for(row_rect: QRect) -> QRect:
    """The card inside a row rect, inset so neighbouring cards show a gap."""
    return row_rect.adjusted(0, CARD_MARGIN_Y, 0, -CARD_MARGIN_Y)


def card_text_rects(card: QRect, font: QFont) -> tuple[QRect, QRect]:
    """Where the title and the offset timecode sit inside a card."""
    title_font = scaled_font(font, CARD_TITLE_DELTA, QFont.Weight.DemiBold)
    offset_font = scaled_font(font, CARD_OFFSET_DELTA)

    left = card.left() + CARD_PAD_X + CARD_NUMBER_WIDTH + CARD_NUMBER_GAP
    width = max(0, card.right() - left - CARD_PAD_X)

    title_height = QFontMetrics(title_font).height()
    offset_height = QFontMetrics(offset_font).height()
    block = title_height + CARD_LINE_GAP + offset_height
    top = card.top() + max(CARD_TITLE_TOP, (card.height() - block) // 2)

    title_rect = QRect(left, top, width, title_height)
    offset_rect = QRect(
        left,
        top + title_height + CARD_LINE_GAP,
        width,
        offset_height,
    )
    return title_rect, offset_rect


def card_path(rect: QRect, round_left: bool, round_right: bool) -> QPainterPath:
    """A card outline rounded on one side only.

    One card is painted by two delegates in two columns, so each half rounds
    the outer corners and leaves the shared seam square. Rounding both halves
    would draw a visible pinch down the middle of every row.
    """
    box = QRectF(rect)
    radius = float(CARD_RADIUS)
    path = QPainterPath()

    if round_left:
        path.moveTo(box.left() + radius, box.top())
    else:
        path.moveTo(box.left(), box.top())

    if round_right:
        path.lineTo(box.right() - radius, box.top())
        path.quadTo(box.right(), box.top(), box.right(), box.top() + radius)
        path.lineTo(box.right(), box.bottom() - radius)
        path.quadTo(box.right(), box.bottom(), box.right() - radius, box.bottom())
    else:
        path.lineTo(box.right(), box.top())
        path.lineTo(box.right(), box.bottom())

    if round_left:
        path.lineTo(box.left() + radius, box.bottom())
        path.quadTo(box.left(), box.bottom(), box.left(), box.bottom() - radius)
        path.lineTo(box.left(), box.top() + radius)
        path.quadTo(box.left(), box.top(), box.left() + radius, box.top())
    else:
        path.lineTo(box.left(), box.bottom())
        path.lineTo(box.left(), box.top())

    path.closeSubpath()
    return path


def card_border_path(rect: QRect, round_left: bool, round_right: bool) -> QPainterPath:
    """The outer edges of a card half, leaving the shared seam unstroked.

    Stroking the fill path instead would draw a border down the middle of every
    row, where the two halves meet and there is no edge to show.
    """
    box = QRectF(rect)
    radius = float(CARD_RADIUS)
    path = QPainterPath()

    if round_left:
        path.moveTo(box.right(), box.top())
        path.lineTo(box.left() + radius, box.top())
        path.quadTo(box.left(), box.top(), box.left(), box.top() + radius)
        path.lineTo(box.left(), box.bottom() - radius)
        path.quadTo(box.left(), box.bottom(), box.left() + radius, box.bottom())
        path.lineTo(box.right(), box.bottom())
        return path

    if round_right:
        path.moveTo(box.left(), box.top())
        path.lineTo(box.right() - radius, box.top())
        path.quadTo(box.right(), box.top(), box.right(), box.top() + radius)
        path.lineTo(box.right(), box.bottom() - radius)
        path.quadTo(box.right(), box.bottom(), box.right() - radius, box.bottom())
        path.lineTo(box.left(), box.bottom())
        return path

    path.addRect(box)
    return path


#: Dot colours for the three states a track can be in.
CARD_STATE_COLOURS = {
    "playing": "#4CE05A",
    "standby": "#EDE04A",
    "idle": "#E03B2C",
}


def _blend(first: QColor, second: QColor, weight: float) -> QColor:
    """``first`` mixed towards ``second``; weight 0 keeps ``first``."""
    keep = 1.0 - weight
    return QColor(
        int(first.red() * keep + second.red() * weight),
        int(first.green() * keep + second.green() * weight),
        int(first.blue() * keep + second.blue() * weight),
        int(first.alpha() * keep + second.alpha() * weight),
    )


def paint_card_surface(
    painter: QPainter,
    card: QRect,
    *,
    round_left: bool,
    round_right: bool,
    selected: bool,
    hovered: bool,
    playing: bool,
) -> None:
    """The glass itself: a lit top edge over a translucent body.

    Qt stylesheets have no backdrop blur, so the glass is built the only way
    that survives here — a vertical gradient that is brightest along the top
    edge, a hairline highlight on that edge, and a border a shade lighter than
    the fill. Painted against the dark page it reads as a lit pane rather than
    a flat rectangle.
    """
    path = card_path(card, round_left, round_right)

    if playing:
        top, bottom = QColor(139, 224, 90, 82), QColor(139, 224, 90, 30)
        border = QColor(139, 224, 90, 150)
    elif selected:
        top, bottom = QColor(255, 255, 255, 56), QColor(255, 255, 255, 20)
        border = QColor(255, 255, 255, 84)
    elif hovered:
        top, bottom = QColor(255, 255, 255, 36), QColor(255, 255, 255, 14)
        border = QColor(255, 255, 255, 54)
    else:
        top, bottom = QColor(255, 255, 255, 23), QColor(255, 255, 255, 8)
        border = QColor(255, 255, 255, 34)

    # Weighted towards the top rather than a straight ramp: real glass catches
    # the light in a band near its upper edge and goes flat below it.
    body = QLinearGradient(card.topLeft(), card.bottomLeft())
    body.setColorAt(0.0, top)
    body.setColorAt(0.42, _blend(top, bottom, 0.72))
    body.setColorAt(1.0, bottom)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(body)
    painter.drawPath(path)

    # The highlight along the top edge is what sells the material; without it
    # the gradient alone just looks like a lighter rectangle.
    painter.save()
    painter.setClipPath(path)
    highlight = QColor(255, 255, 255, 120 if (selected or playing) else 76)
    painter.setPen(QPen(highlight, 1.0))
    painter.drawLine(
        card.left(),
        card.top() + 1,
        card.right(),
        card.top() + 1,
    )
    painter.restore()

    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(border, 1.0))
    painter.drawPath(card_border_path(card, round_left, round_right))


class PlaylistCardDelegate(QStyledItemDelegate):
    """Draws each playlist row as a card: number, title, offset, state dot.

    The row is one card spread over two columns. This delegate owns the left
    column and paints everything readable; :class:`PlaylistOffsetDelegate`
    owns the right column and paints only the state dot, because the offset
    column has to stay a real editable cell for the timecode editor to open
    into, even though its value is displayed here under the title.
    """

    def paint(self, painter: QPainter, option, index) -> None:
        table = self.parent()
        card = card_rect_for(option.rect)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        state = table.row_state(index.row()) if table is not None else "idle"
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        paint_card_surface(
            painter,
            card,
            round_left=True,
            round_right=False,
            selected=selected,
            hovered=hovered,
            playing=state == "playing",
        )

        base_font = option.font
        title_rect, offset_rect = card_text_rects(card, base_font)

        # -- the row number, quiet enough to scan past ----------------------
        painter.setFont(scaled_font(base_font, CARD_NUMBER_DELTA, QFont.Weight.Medium))
        painter.setPen(QColor(255, 255, 255, 110 if not selected else 165))
        # Right-aligned so the numbers form a column against the titles rather
        # than drifting left as the list passes ten.
        painter.drawText(
            QRect(
                card.left() + CARD_PAD_X,
                card.top(),
                CARD_NUMBER_WIDTH,
                card.height(),
            ),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
            f"{index.row() + 1}.",
        )

        missing = bool(index.data(PLAYLIST_MISSING_ROLE))
        title = str(index.data(PLAYLIST_TITLE_ROLE) or "").strip()
        if not title:
            raw = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            title = raw.split(" — ")[0].strip() or raw

        # -- load state, kept from the old caption --------------------------
        # The old row put "— READY", "— 45%" or "— MISSING" in the title. That
        # is real information about whether a track can be fired, so it moves
        # to its own chip rather than disappearing with the caption.
        badge = "MISSING" if missing else self._load_badge(index)
        badge_font = scaled_font(base_font, CARD_BADGE_DELTA, QFont.Weight.DemiBold)
        badge_width = 0
        if badge:
            badge_width = QFontMetrics(badge_font).horizontalAdvance(badge) + 8

        # -- title -----------------------------------------------------------
        title_font = scaled_font(base_font, CARD_TITLE_DELTA, QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(
            QColor("#FFB865") if missing else QColor(255, 255, 255, 255 if selected else 232)
        )
        text_width = max(0, title_rect.width() - badge_width)
        painter.drawText(
            QRect(title_rect.left(), title_rect.top(), text_width, title_rect.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            QFontMetrics(title_font).elidedText(
                title,
                Qt.TextElideMode.ElideRight,
                text_width,
            ),
        )

        if badge:
            painter.setFont(badge_font)
            painter.setPen(
                QColor("#FFB865") if missing else QColor(255, 255, 255, 120)
            )
            painter.drawText(
                QRect(
                    title_rect.right() - badge_width,
                    title_rect.top(),
                    badge_width,
                    title_rect.height(),
                ),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                badge,
            )

        # -- offset timecode, under the title --------------------------------
        offset_index = index.sibling(index.row(), PlaylistDropListWidget.OFFSET_COLUMN)
        offset_text = str(offset_index.data(Qt.ItemDataRole.DisplayRole) or "00:00:00:00")
        painter.setFont(scaled_font(base_font, CARD_OFFSET_DELTA))
        painter.setPen(QColor(255, 255, 255, 150 if selected else 118))
        painter.drawText(
            offset_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            offset_text,
        )

        painter.restore()

    @staticmethod
    def _load_badge(index) -> str:
        raw = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if " — " not in raw:
            return ""
        suffix = raw.rsplit(" — ", 1)[1].strip()
        # A fully loaded track is the normal case; saying so on every row is
        # noise that makes the rows that are not ready harder to spot.
        if suffix in {"READY", "100%"}:
            return ""
        return suffix

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(CARD_ROW_HEIGHT)
        return size


class PlaylistOffsetDelegate(QStyledItemDelegate):
    """Stable HH:MM:SS:FF editor for the Playlist Offset column.

    Deliberately does not use QLineEdit.inputMask(). The mask caused the
    caret to rewrite neighbouring digits when users clicked inside an
    existing timecode. Validation is performed only when the edit commits.

    Since the playlist became a row of cards this also paints the right end of
    the card and the state dot. The offset value itself is drawn by
    :class:`PlaylistCardDelegate` under the title, where a reader expects it.
    """

    edit_started = Signal(int)
    edit_finished = Signal(int)

    def paint(self, painter: QPainter, option, index) -> None:
        table = self.parent()
        card = card_rect_for(option.rect)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        state = table.row_state(index.row()) if table is not None else "idle"
        paint_card_surface(
            painter,
            card,
            round_left=False,
            round_right=True,
            selected=bool(option.state & QStyle.StateFlag.State_Selected),
            hovered=bool(option.state & QStyle.StateFlag.State_MouseOver),
            playing=state == "playing",
        )

        colour = QColor(CARD_STATE_COLOURS.get(state, CARD_STATE_COLOURS["idle"]))
        centre = QRectF(card).center()

        # A plain dot, no halo. The halo that used to sit behind it read as a
        # second, larger indicator and took a quarter of the row's height to
        # say what the colour alone already says.
        radius = CARD_DOT_SIZE / 2.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(centre, radius, radius)
        painter.restore()

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setObjectName("playlistOffsetEditor")
        editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        editor.setFrame(False)
        editor.setMaxLength(11)
        editor.setPlaceholderText("00:00:00:00")
        editor.setContentsMargins(0, 0, 0, 0)
        # This editor must not inherit Tracyy's global QLineEdit padding and
        # rounded-corner style. That global style made the in-cell editor look
        # like a tiny green pill and clipped the timecode text.
        editor.setStyleSheet(
            """
            QLineEdit#playlistOffsetEditor {
                background: #0B1007;
                color: #FFFFFF;
                border: 1px solid #55E314;
                border-radius: 2px;
                padding: 0px 5px;
                margin: 0px;
                selection-background-color: #2B7400;
                selection-color: #FFFFFF;
            }
            """
        )
        editor.setProperty("playlistOffsetRow", int(index.row()))
        self.edit_started.emit(int(index.row()))
        return editor

    def updateEditorGeometry(self, editor, option, index) -> None:
        # Open the editor over the timecode as it is drawn — under the title,
        # in the neighbouring column — rather than over this cell, which now
        # holds the state dot. Editing in one place and reading in another is
        # how an offset gets typed into the wrong row.
        table = self.parent()
        if table is None:
            editor.setGeometry(option.rect.adjusted(1, 1, -1, -1))
            return

        row_rect = table.visualRect(
            index.sibling(index.row(), table.PLAYLIST_COLUMN)
        )
        _title_rect, offset_rect = card_text_rects(
            card_rect_for(row_rect),
            table.font(),
        )
        editor.setGeometry(offset_rect.adjusted(-4, -3, 4, 3))

    def setEditorData(self, editor, index) -> None:
        value = str(index.data(Qt.ItemDataRole.DisplayRole) or "00:00:00:00")
        editor.setText(value)
        # Select after Qt finishes delivering the double-click, otherwise
        # the second mouse event can immediately move the caret again.
        QTimer.singleShot(0, editor.selectAll)

    def setModelData(self, editor, model, index) -> None:
        model.setData(
            index,
            str(editor.text() or "").strip(),
            Qt.ItemDataRole.EditRole,
        )
        self.edit_finished.emit(int(index.row()))

    def destroyEditor(self, editor, index) -> None:
        self.edit_finished.emit(int(index.row()))
        super().destroyEditor(editor, index)


class PlaylistDropListWidget(QTableWidget):
    """Two-column playlist with a QListWidget-compatible API.

    Internal drag/drop is fully owned by Tracyy. QTableWidget's native
    MoveAction is deliberately bypassed because combining a manual row move
    with Qt's native move completion can delete the source row twice.
    """

    external_paths_dropped = Signal(list)
    rows_reordered = Signal(int, int)
    offset_edit_state_changed = Signal(bool, int)

    PLAYLIST_COLUMN = 0
    OFFSET_COLUMN = 1
    INTERNAL_ROW_MIME = "application/x-tracyy-playlist-row"

    def __init__(self, parent=None) -> None:
        super().__init__(0, 2, parent)
        self._drag_source_row = -1
        self._offset_editing_row = -1

        self._playing_row = -1
        self._standby_row = -1
        self._press_pos = None

        self.setObjectName("playlistTable")
        self.setHorizontalHeaderLabels(["Playlist", "Offset"])
        self.verticalHeader().hide()
        # The cards carry their own labelling — a number, a title and a
        # timecode each — so a header naming two columns describes a layout
        # that is no longer there.
        self.horizontalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(CARD_ROW_HEIGHT)
        self.setShowGrid(False)
        self.setMouseTracking(True)
        self.setWordWrap(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Editing is opened manually from MainWindow. This prevents a
        # single click/selection change from accidentally entering edit.
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        self._card_delegate = PlaylistCardDelegate(self)
        self.setItemDelegateForColumn(
            self.PLAYLIST_COLUMN,
            self._card_delegate,
        )

        self._offset_delegate = PlaylistOffsetDelegate(self)
        self._offset_delegate.edit_started.connect(self._on_offset_edit_started)
        self._offset_delegate.edit_finished.connect(self._on_offset_edit_finished)
        self.setItemDelegateForColumn(
            self.OFFSET_COLUMN,
            self._offset_delegate,
        )

        self.setAcceptDrops(True)
        self.setDragDropOverwriteMode(False)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(
            self.PLAYLIST_COLUMN,
            QHeaderView.ResizeMode.Stretch,
        )
        header.setSectionResizeMode(
            self.OFFSET_COLUMN,
            QHeaderView.ResizeMode.Fixed,
        )
        self.setColumnWidth(self.OFFSET_COLUMN, CARD_STATE_WIDTH)

    # --- row state, which decides the dot colour ---
    def set_row_states(self, playing_row: int, standby_row: int) -> None:
        """Tell the cards which track is playing and which is armed.

        Pushed in from the controller rather than worked out here: the widget
        knows about rows, and which row is playing is a fact about paths that
        survives a drag-and-drop reorder.
        """
        playing = int(playing_row)
        standby = int(standby_row)
        if playing == self._playing_row and standby == self._standby_row:
            return
        self._playing_row = playing
        self._standby_row = standby
        self.viewport().update()

    def row_state(self, row: int) -> str:
        if int(row) == self._playing_row:
            return "playing"
        if int(row) == self._standby_row:
            return "standby"
        return "idle"

    def pressed_zone(self, row: int) -> str:
        """Which line of the card the last press landed on.

        Both lines live in one cell now, so the double-click handler cannot
        tell "rename the track" from "edit the offset" by column alone.
        """
        position = self._press_pos
        index = self.model().index(int(row), self.PLAYLIST_COLUMN)
        if position is None or not index.isValid():
            return "title"
        card = card_rect_for(self.visualRect(index))
        _title_rect, offset_rect = card_text_rects(card, self.font())
        return "offset" if position.y() >= offset_rect.top() - 2 else "title"

    # --- offset edit state ---
    def _on_offset_edit_started(self, row: int) -> None:
        self._offset_editing_row = int(row)
        self.offset_edit_state_changed.emit(True, int(row))

    def _on_offset_edit_finished(self, row: int) -> None:
        if self._offset_editing_row == int(row):
            self._offset_editing_row = -1
        self.offset_edit_state_changed.emit(False, int(row))

    def is_offset_editing(self, row: int | None = None) -> bool:
        if row is None:
            return self._offset_editing_row >= 0
        return self._offset_editing_row == int(row)

    def begin_offset_edit(self, row: int) -> None:
        if row < 0 or row >= self.rowCount():
            return
        item = super().item(row, self.OFFSET_COLUMN)
        if item is None:
            return
        self.setCurrentCell(row, self.OFFSET_COLUMN)
        self.selectRow(row)
        self.editItem(item)

    # --- QListWidget compatibility used throughout MainWindow ---
    def count(self) -> int:
        return self.rowCount()

    def item(self, row: int, column: int = 0):
        return super().item(row, column)

    def currentItem(self):
        row = self.currentRow()
        if row < 0:
            return None
        return super().item(row, self.PLAYLIST_COLUMN)

    def setCurrentRow(self, row: int) -> None:
        if row < 0 or row >= self.rowCount():
            self.clearSelection()
            self.setCurrentCell(-1, -1)
            return
        self.setCurrentCell(row, self.PLAYLIST_COLUMN)
        self.selectRow(row)

    def clear(self) -> None:
        self._offset_editing_row = -1
        self.setRowCount(0)

    def addItem(
        self,
        item: QTableWidgetItem,
        offset_text: str = "00:00:00:00",
    ) -> None:
        row = self.rowCount()
        self.insertRow(row)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        super().setItem(row, self.PLAYLIST_COLUMN, item)
        self.set_offset_for_row(row, offset_text)

    def takeItem(self, row: int, column: int | None = None):
        if column is not None:
            return super().takeItem(row, column)
        if row < 0 or row >= self.rowCount():
            return None
        playlist_item = super().takeItem(row, self.PLAYLIST_COLUMN)
        super().takeItem(row, self.OFFSET_COLUMN)
        self.removeRow(row)
        return playlist_item

    def set_offset_for_row(self, row: int, text: str) -> None:
        if row < 0 or row >= self.rowCount():
            return
        # Never rewrite the model under a live editor. That was the main
        # source of the "numbers jumping" behaviour.
        if self.is_offset_editing(row):
            return

        offset_item = super().item(row, self.OFFSET_COLUMN)
        if offset_item is None:
            offset_item = QTableWidgetItem()
            # The flag, not int(flag): PySide6 deprecated the integer overload,
            # and the project's warning policy turns that into an exception —
            # which load_project_path swallows into "could not open project".
            offset_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            offset_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsEditable
            )
            super().setItem(row, self.OFFSET_COLUMN, offset_item)

        offset_item.setText(str(text or "00:00:00:00"))
        source_item = super().item(row, self.PLAYLIST_COLUMN)
        if source_item is not None:
            offset_item.setData(
                Qt.ItemDataRole.UserRole,
                source_item.data(Qt.ItemDataRole.UserRole),
            )
        offset_item.setToolTip("Double-click to edit Offset (HH:MM:SS:FF)")

    @staticmethod
    def local_drop_paths(event) -> list[str]:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        paths: list[str] = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            local_path = str(url.toLocalFile()).strip()
            if not local_path:
                continue
            path = Path(local_path)
            if path.is_dir():
                paths.append(str(path))
                continue
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                paths.append(str(path))
        return paths

    def mousePressEvent(self, event) -> None:
        try:
            point = event.position().toPoint()
        except Exception:
            point = event.pos()
        index = self.indexAt(point)
        self._drag_source_row = index.row()
        # Kept for pressed_zone: a double click arrives as two presses, so the
        # position of the last one still describes where the user aimed.
        self._press_pos = point
        super().mousePressEvent(event)

    def startDrag(self, supported_actions) -> None:
        """Start a Tracyy-owned row drag without QTableWidget moving items.

        Calling QAbstractItemView.startDrag() with MoveAction makes Qt remove
        the source item after drop completion. Tracyy already performs the row
        reorder in dropEvent(), so using the native implementation would apply
        the move twice and can make the dragged song disappear.
        """
        row = self.currentRow()
        if row < 0 or row >= self.rowCount():
            return

        source_item = super().item(row, self.PLAYLIST_COLUMN)
        if source_item is None:
            return

        self._drag_source_row = row

        mime = QMimeData()
        mime.setData(
            self.INTERNAL_ROW_MIME,
            str(row).encode("ascii", errors="ignore"),
        )
        # Human-readable text is useful for platform drag feedback, but the
        # private MIME type above is the only value used to reorder rows.
        mime.setText(str(source_item.text() or ""))

        drag = QDrag(self)
        drag.setMimeData(mime)
        # Do not call super().startDrag(). This custom QDrag has no native
        # QTableWidget source-removal step; dropEvent() is the single owner of
        # the reorder transaction.
        drag.exec(
            Qt.DropAction.MoveAction,
            Qt.DropAction.MoveAction,
        )

    def dragEnterEvent(self, event) -> None:
        if self.local_drop_paths(event):
            event.acceptProposedAction()
            return
        if (
            event.source() is self
            and event.mimeData() is not None
            and event.mimeData().hasFormat(self.INTERNAL_ROW_MIME)
        ):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if self.local_drop_paths(event):
            event.acceptProposedAction()
            return
        if (
            event.source() is self
            and event.mimeData() is not None
            and event.mimeData().hasFormat(self.INTERNAL_ROW_MIME)
        ):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        super().dragMoveEvent(event)

    def _drop_target_row(self, event) -> int:
        try:
            point = event.position().toPoint()
        except Exception:
            point = event.pos()
        index = self.indexAt(point)
        if not index.isValid():
            return self.rowCount()
        target = index.row()
        rect = self.visualRect(index)
        if point.y() > rect.center().y():
            target += 1
        return max(0, min(target, self.rowCount()))

    def dropEvent(self, event) -> None:
        paths = self.local_drop_paths(event)
        if paths:
            self.external_paths_dropped.emit(paths)
            event.acceptProposedAction()
            return

        mime = event.mimeData()
        if (
            event.source() is not self
            or mime is None
            or not mime.hasFormat(self.INTERNAL_ROW_MIME)
        ):
            super().dropEvent(event)
            return

        source_row = int(self._drag_source_row)
        try:
            encoded_row = bytes(mime.data(self.INTERNAL_ROW_MIME))
            source_row = int(encoded_row.decode("ascii"))
        except Exception:
            pass

        if source_row < 0 or source_row >= self.rowCount():
            event.ignore()
            return

        target_row = self._drop_target_row(event)
        if target_row in (source_row, source_row + 1):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return

        # One transaction owns the move: snapshot both cells, remove exactly
        # one source row, then insert exactly one destination row. The custom
        # startDrag() above guarantees Qt will not perform a second deletion.
        source_playlist = super().item(source_row, self.PLAYLIST_COLUMN)
        source_offset = super().item(source_row, self.OFFSET_COLUMN)
        if source_playlist is None:
            event.ignore()
            return

        playlist_clone = source_playlist.clone()
        offset_clone = (
            source_offset.clone()
            if source_offset is not None
            else QTableWidgetItem("00:00:00:00")
        )

        self.blockSignals(True)
        self.setUpdatesEnabled(False)
        try:
            self.removeRow(source_row)
            if target_row > source_row:
                target_row -= 1
            target_row = max(0, min(target_row, self.rowCount()))
            self.insertRow(target_row)
            super().setItem(
                target_row,
                self.PLAYLIST_COLUMN,
                playlist_clone,
            )
            super().setItem(
                target_row,
                self.OFFSET_COLUMN,
                offset_clone,
            )
            self.setCurrentCell(target_row, self.PLAYLIST_COLUMN)
            self.selectRow(target_row)
        finally:
            self.setUpdatesEnabled(True)
            self.blockSignals(False)
            self.viewport().update()

        self._drag_source_row = target_row
        self.rows_reordered.emit(source_row, target_row)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
