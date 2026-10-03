from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QEvent,
    QMimeData,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDrag,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from tracyy.core.ids import cue_key

from .constants import MARKER_SHAPE_OPTIONS, normalize_marker_shape

if TYPE_CHECKING:  # pragma: no cover - import cycle, annotations only
    from tracyy.ui.main_window import MainWindow


#: Carries the dragged card's position between the card and the grid.
CUE_TYPE_CARD_MIME = "application/x-tracyy-cue-type-card"


class CueTypeCard(QFrame):
    """One cue type, and the handle for dragging it to a new position.

    A press that lands on a button or the name field never reaches here, so
    the card drags from its background and its labels only — the controls stay
    clickable without a separate drag handle taking up room.
    """

    def __init__(self, position: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.position = int(position)
        self._press_pos = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            try:
                self._press_pos = event.position().toPoint()
            except Exception:
                self._press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press_pos is None:
            super().mouseMoveEvent(event)
            return

        try:
            point = event.position().toPoint()
        except Exception:
            point = event.pos()

        threshold = QApplication.startDragDistance()
        if (point - self._press_pos).manhattanLength() < threshold:
            super().mouseMoveEvent(event)
            return

        self._press_pos = None

        mime = QMimeData()
        mime.setData(CUE_TYPE_CARD_MIME, str(self.position).encode("ascii"))

        drag = QDrag(self)
        drag.setMimeData(mime)
        # The card itself is the drag image, so the pointer carries the thing
        # being moved rather than a generic rectangle.
        drag.setPixmap(self.grab())
        drag.setHotSpot(point)
        drag.exec(Qt.DropAction.MoveAction)

    def mouseReleaseEvent(self, event) -> None:
        self._press_pos = None
        super().mouseReleaseEvent(event)


class CueTypeCardGrid(QWidget):
    """The card container, and the only thing that knows where a drop landed."""

    card_move_requested = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData() is not None and event.mimeData().hasFormat(CUE_TYPE_CARD_MIME):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData() is not None and event.mimeData().hasFormat(CUE_TYPE_CARD_MIME):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        if mime is None or not mime.hasFormat(CUE_TYPE_CARD_MIME):
            event.ignore()
            return

        try:
            source = int(bytes(mime.data(CUE_TYPE_CARD_MIME)).decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            event.ignore()
            return

        try:
            point = event.position().toPoint()
        except Exception:
            point = event.pos()

        target = self._card_at(point)
        if target is None or target == source:
            event.ignore()
            return

        self.card_move_requested.emit(source, target)
        event.acceptProposedAction()

    def _card_at(self, point) -> int | None:
        """Which card is under ``point``, by geometry rather than by hit-test.

        childAt() returns whichever label or button happens to be under the
        cursor, and walking back up from there breaks whenever the card gains a
        nested layout. The cards are few and their rectangles are exact.
        """
        for card in self.findChildren(CueTypeCard):
            if card.geometry().contains(point):
                return card.position
        return None


class CueTypeEditorDialog(QDialog):
    def __init__(self, main_window: MainWindow) -> None:
        super().__init__(main_window)
        self.main_window = main_window
        self.setWindowTitle("Customise Cue Point Types")
        self.resize(900, 700)
        self._building_table = False
        self._build_ui()
        self.refresh_table()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        heading = QLabel("Cue Point Types")
        heading.setObjectName("cueTypeDialogTitle")
        subheading = QLabel(
            "Kéo thẻ để đổi thứ tự · sửa tên ngay trên thẻ · màu áp cho marker và cue viewer"
        )
        subheading.setObjectName("cueTypeDialogSubtitle")
        subheading.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(subheading)
        layout.addSpacing(10)

        self.cards = CueTypeCardGrid()
        self.cards_layout = QGridLayout(self.cards)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setHorizontalSpacing(10)
        self.cards_layout.setVerticalSpacing(10)

        scroll = QScrollArea()
        scroll.setObjectName("cueTypeScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.cards)
        layout.addWidget(scroll, 1)

        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 6, 0, 0)
        self.new_type_button = QPushButton("+  New Type")
        self.new_type_button.setObjectName("cueTypeSecondaryButton")
        self.new_type_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.done_button = QPushButton("Done")
        self.done_button.setObjectName("cueTypePrimaryButton")
        self.done_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.done_button.setDefault(True)
        bottom_row.addWidget(self.new_type_button)
        bottom_row.addStretch()
        bottom_row.addWidget(self.done_button)
        layout.addLayout(bottom_row)

        self.new_type_button.clicked.connect(self.on_new_type)
        self.done_button.clicked.connect(self.on_done)
        self.cards.card_move_requested.connect(self.on_row_move_requested)

    #: Cards per row. Two keeps a card wide enough for five shape buttons and
    #: three flag pills without either group wrapping.
    CARDS_PER_ROW = 2

    def refresh_table(self) -> None:
        """Rebuild every card. Named for the table it replaced — the handlers
        all call it by this name and renaming them buys nothing."""
        self._building_table = True
        try:
            mw = self.main_window
            mw.ensure_cue_type_meta()
            cue_defs = list(mw.cue_type_defs)

            while self.cards_layout.count():
                item = self.cards_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    # setParent(None) before deleteLater: until the event loop
                    # runs the deletion the old card still has a parent and
                    # still paints where it was.
                    widget.setParent(None)
                    widget.deleteLater()

            for position, (name, color) in enumerate(cue_defs):
                meta = mw.cue_type_meta.get(
                    name,
                    {
                        "visible": True,
                        "locked": False,
                        "sync": False,
                        "marker_shape": "rectangle",
                    },
                )
                card = self.build_type_card(position, name, color, meta)
                self.cards_layout.addWidget(
                    card,
                    position // self.CARDS_PER_ROW,
                    position % self.CARDS_PER_ROW,
                )

            # Soaks up the leftover height so the cards stay at the top instead
            # of stretching to fill a half-empty dialog.
            self.cards_layout.setRowStretch(self.cards_layout.rowCount(), 1)
            for column in range(self.CARDS_PER_ROW):
                self.cards_layout.setColumnStretch(column, 1)
        finally:
            self._building_table = False

    def build_type_card(
        self,
        position: int,
        name: str,
        color: str,
        meta: dict,
    ) -> QWidget:
        visible = bool(meta.get("visible", True))

        card = CueTypeCard(position)
        card.setObjectName("cueTypeCard")
        card.setProperty("visible_state", "true" if visible else "false")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 13, 14, 14)
        card_layout.setSpacing(11)

        # -- header: colour, name, hex, delete ------------------------------
        header = QHBoxLayout()
        header.setSpacing(10)

        colour_button = QPushButton()
        colour_button.setObjectName("cueTypeSwatch")
        colour_button.setFixedSize(20, 20)
        colour_button.setIcon(self._colour_swatch(color, 20, 20, dim=not visible))
        colour_button.setIconSize(QSize(20, 20))
        colour_button.setCursor(Qt.CursorShape.PointingHandCursor)
        colour_button.clicked.connect(
            lambda _checked=False, cue_name=name: self.on_change_color(cue_name)
        )

        name_edit = QLineEdit(name)
        name_edit.setObjectName("cueTypeName")
        name_edit.setProperty("visible_state", "true" if visible else "false")
        name_edit.editingFinished.connect(
            lambda cue_name=name, edit=name_edit: self.on_name_edited(cue_name, edit)
        )

        hex_label = QLabel(str(color).upper())
        hex_label.setObjectName("cueTypeHex")

        delete_button = QPushButton()
        delete_button.setObjectName("cueTypeDelete")
        delete_button.setFixedSize(22, 22)
        delete_button.setIcon(self._close_glyph(10))
        delete_button.setIconSize(QSize(10, 10))
        delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_button.clicked.connect(
            lambda _checked=False, cue_name=name: self.on_delete_type(cue_name)
        )

        header.addWidget(colour_button)
        header.addWidget(name_edit, 1)
        header.addWidget(hex_label)
        header.addWidget(delete_button)
        card_layout.addLayout(header)

        # -- the three flags, as pills that read on or off at a glance ------
        flags = QHBoxLayout()
        flags.setSpacing(6)
        for label, key, handler in (
            ("VISIBLE", "visible", self.on_visible_changed),
            ("LOCKED", "locked", self.on_locked_changed),
            ("SYNC", "sync", self.on_sync_changed),
        ):
            pill = QPushButton(label)
            pill.setObjectName("cueTypeFlag")
            pill.setCheckable(True)
            pill.setChecked(bool(meta.get(key, key == "visible")))
            pill.setCursor(Qt.CursorShape.PointingHandCursor)
            pill.toggled.connect(
                lambda checked, cue_name=name, sink=handler, flag=key: self._flag_toggled(
                    sink,
                    flag,
                    cue_name,
                    checked,
                )
            )
            flags.addWidget(pill, 1)
        card_layout.addLayout(flags)

        # -- marker shape: all five shown, in the type's own colour ---------
        shape_row = QHBoxLayout()
        shape_row.setSpacing(6)

        shape_caption = QLabel("MARKER")
        shape_caption.setObjectName("cueTypeCaption")
        shape_caption.setFixedWidth(54)
        shape_row.addWidget(shape_caption)

        current_shape = normalize_marker_shape(meta.get("marker_shape", "rectangle"))
        group = QButtonGroup(card)
        group.setExclusive(True)
        for shape_label, shape_value in MARKER_SHAPE_OPTIONS:
            chosen = shape_value == current_shape
            button = QPushButton()
            button.setObjectName("cueTypeShape")
            button.setCheckable(True)
            button.setChecked(chosen)
            button.setFixedSize(28, 26)
            button.setIcon(
                self._shape_glyph(shape_value, color if chosen else "#4A5741", 14)
            )
            button.setIconSize(QSize(14, 14))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(shape_label)
            button.clicked.connect(
                lambda _checked=False, cue_name=name, value=shape_value: (
                    self.on_marker_shape_changed(cue_name, value)
                )
            )
            group.addButton(button)
            shape_row.addWidget(button)

        shape_row.addStretch()
        card_layout.addLayout(shape_row)

        return card

    def _flag_toggled(self, sink, key: str, cue_name: str, checked: bool) -> None:
        if self._building_table:
            return
        sink(cue_name, bool(checked))
        # Visible dims its own card, so that one has to be redrawn. Compared on
        # the key, never on the handler: `self.method is self.method` is False,
        # because each attribute access builds a fresh bound method, and the
        # card silently stopped dimming.
        if key == "visible":
            self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        """Rebuild the cards once the current signal has finished delivering.

        Every one of these refreshes is triggered by a widget that lives on a
        card, and rebuilding destroys that widget. Doing it inline would tear
        down the object Qt is still emitting from.
        """
        QTimer.singleShot(0, self.refresh_table)

    def _pixmap(self, width: int, height: int) -> QPixmap:
        """A transparent pixmap at the screen's real pixel ratio.

        Painting happens in logical units afterwards: Qt applies the ratio to
        the pixmap itself, so scaling again here would double it.
        """
        try:
            ratio = max(1.0, float(self.devicePixelRatioF()))
        except Exception:
            ratio = 2.0
        pixmap = QPixmap(round(width * ratio), round(height * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        return pixmap

    def _colour_swatch(
        self,
        colour: str,
        width: int = 14,
        height: int = 14,
        dim: bool = False,
    ) -> QIcon:
        pixmap = self._pixmap(width, height)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            fill = QColor(colour)
            if dim:
                fill.setAlpha(115)
            painter.setPen(QPen(QColor(255, 255, 255, 46), 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(
                QRectF(0.5, 0.5, width - 1.0, height - 1.0), 5.0, 5.0
            )
        finally:
            painter.end()
        return QIcon(pixmap)

    def _shape_path(self, shape: str) -> QPainterPath:
        """The marker outline, in a 16-unit box.

        No scaling happens here on purpose — the caller scales its painter, and
        a second scale in the path would apply the size twice.
        """
        path = QPainterPath()

        if shape == "triangle":
            path.moveTo(8.0, 1.4)
            path.lineTo(15.2, 14.4)
            path.lineTo(0.8, 14.4)
            path.closeSubpath()
        elif shape == "diamond":
            path.moveTo(8.0, 0.6)
            path.lineTo(15.4, 8.0)
            path.lineTo(8.0, 15.4)
            path.lineTo(0.6, 8.0)
            path.closeSubpath()
        elif shape == "circle":
            path.addEllipse(QRectF(0.8, 0.8, 14.4, 14.4))
        elif shape == "heart":
            path.moveTo(8.0, 14.6)
            path.cubicTo(2.0, 10.4, 1.3, 7.5, 1.3, 5.7)
            path.cubicTo(1.3, 3.5, 2.9, 1.9, 5.1, 1.9)
            path.cubicTo(6.4, 1.9, 7.5, 2.7, 8.0, 3.8)
            path.cubicTo(8.5, 2.7, 9.6, 1.9, 10.9, 1.9)
            path.cubicTo(13.1, 1.9, 14.7, 3.5, 14.7, 5.7)
            path.cubicTo(14.7, 7.5, 14.0, 10.4, 8.0, 14.6)
            path.closeSubpath()
        else:
            path.addRoundedRect(QRectF(0.8, 2.4, 14.4, 11.2), 2.0, 2.0)

        return path

    def _shape_glyph(self, shape: str, colour: str, size: int = 14) -> QIcon:
        pixmap = self._pixmap(size, size)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Logical units: the pixmap already carries the device ratio, so
            # scaling by the device size here would apply it twice.
            painter.scale(size / 16.0, size / 16.0)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colour))
            painter.drawPath(self._shape_path(shape))
        finally:
            painter.end()
        return QIcon(pixmap)

    def _close_glyph(self, size: int = 12) -> QIcon:
        pixmap = self._pixmap(size, size)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(
                QPen(
                    QColor("#9AA291"),
                    1.6,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            inset = 2.5
            painter.drawLine(
                QPointF(inset, inset),
                QPointF(size - inset, size - inset),
            )
            painter.drawLine(
                QPointF(size - inset, inset),
                QPointF(inset, size - inset),
            )
        finally:
            painter.end()
        return QIcon(pixmap)

    def on_marker_shape_changed(
        self,
        cue_name: str,
        marker_shape: str,
    ) -> None:
        if self._building_table:
            return
        self.main_window.set_cue_type_marker_shape(
            cue_name,
            marker_shape,
        )
        # Redrawn so the chosen shape takes the type's colour and the others
        # go back to grey.
        self._schedule_refresh()

    def on_new_type(self) -> None:
        self.main_window.add_cue_type()
        self.refresh_table()

    def on_done(self) -> None:
        self.apply_row_order()
        self.accept()

    def on_visible_changed(self, cue_name: str, visible: bool) -> None:
        self.main_window.set_cue_type_visible(cue_name, visible)

    def on_locked_changed(self, cue_name: str, locked: bool) -> None:
        self.main_window.set_cue_type_locked(cue_name, locked)

    def on_sync_changed(self, cue_name: str, sync_value: bool) -> None:
        self.main_window.set_cue_type_sync(cue_name, sync_value)

    def on_change_color(self, cue_name: str) -> None:
        self.main_window.active_cue_type = cue_name
        self.main_window.change_active_cue_type_color()
        self._schedule_refresh()

    def on_delete_type(self, cue_name: str) -> None:
        self.main_window.delete_cue_type(cue_name)
        self._schedule_refresh()

    def on_name_edited(self, old_name: str, edit: QLineEdit) -> None:
        """Commit a rename, or put the old name back and say why not.

        Every rejection restores the field rather than leaving the typed text
        sitting there: a name that was refused but still on screen reads as
        saved, and the next thing the user does is close the dialog believing
        it was.
        """
        if self._building_table:
            return

        old_name = str(old_name or "").strip()
        new_name = str(edit.text() or "").strip()

        if not old_name:
            return

        def restore() -> None:
            self._building_table = True
            edit.setText(old_name)
            self._building_table = False

        if not new_name:
            restore()
            return

        if new_name == old_name:
            return

        if self.main_window.cue_type_is_locked(old_name):
            restore()
            self.main_window.show_error("Cue type này đang bị khóa.")
            return

        if any(
            existing_name == new_name
            for existing_name, _color in self.main_window.cue_type_defs
            if existing_name != old_name
        ):
            restore()
            self.main_window.show_error("Tên cue type này đã tồn tại.")
            return

        self.main_window.rename_cue_type(old_name, new_name)
        self._schedule_refresh()

    def on_row_move_requested(
        self,
        source_row: int,
        target_row: int,
    ) -> None:
        cue_defs = list(self.main_window.cue_type_defs)

        if not 0 <= source_row < len(cue_defs):
            return

        target_row = max(0, min(target_row, len(cue_defs) - 1))

        moved = cue_defs.pop(source_row)
        cue_defs.insert(target_row, moved)

        ordered_names = [name for name, _color in cue_defs]
        self.main_window.reorder_cue_types(ordered_names)
        self.refresh_table()

    def apply_row_order(self) -> None:
        # Ordering is applied immediately by on_row_move_requested().
        return


class CueTableDelegate(QStyledItemDelegate):
    editor_started = Signal(int, int)

    PROGRESS_ROLE = int(Qt.ItemDataRole.UserRole) + 20
    COUNTDOWN_ROLE = int(Qt.ItemDataRole.UserRole) + 21
    COLOR_ROLE = int(Qt.ItemDataRole.UserRole) + 22

    def createEditor(
        self,
        parent,
        option,
        index,
    ):
        editor = super().createEditor(
            parent,
            option,
            index,
        )
        if editor is not None:
            self.editor_started.emit(
                int(index.row()),
                int(index.column()),
            )
        return editor

    def fitted_font(
        self,
        base_font: QFont,
        text: str,
        available_width: int,
    ) -> QFont:
        font = QFont(base_font)
        if not text or available_width <= 8:
            return font

        size = max(7, round(font.pointSizeF() or 12))
        while size > 7:
            font.setPointSize(size)
            metrics = QFontMetrics(font)
            if metrics.horizontalAdvance(text) <= available_width:
                break
            size -= 1

        return font

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index,
    ) -> None:
        progress_value = index.data(self.PROGRESS_ROLE)
        countdown_text = index.data(self.COUNTDOWN_ROLE)

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        if countdown_text and index.column() == 0:
            opt.text = str(countdown_text)

        style = opt.widget.style() if opt.widget is not None else QApplication.style()

        text = opt.text
        opt.text = ""

        painter.save()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            opt,
            painter,
            opt.widget,
        )

        if progress_value is not None and index.column() == 3:
            try:
                progress = max(0.0, min(1.0, float(progress_value)))
            except (TypeError, ValueError):
                progress = 0.0

            color_value = index.data(self.COLOR_ROLE)
            cue_color = QColor(str(color_value or "#b34b5d"))
            cue_color.setAlpha(115)

            fill_rect = option.rect.adjusted(2, 3, -2, -3)
            fill_rect.setWidth(round(fill_rect.width() * progress))
            if fill_rect.width() > 0:
                painter.fillRect(fill_rect, cue_color)

        text_rect = option.rect.adjusted(6, 0, -6, 0)
        if index.column() == 2:
            text_rect = option.rect.adjusted(4, 0, -4, 0)

        foreground = index.data(Qt.ItemDataRole.ForegroundRole)
        if foreground is not None:
            try:
                painter.setPen(foreground.color())
            except Exception:
                painter.setPen(option.palette.text().color())
        else:
            painter.setPen(option.palette.text().color())

        fitted = self.fitted_font(
            option.font,
            text,
            max(12, text_rect.width() - 2),
        )
        painter.setFont(fitted)
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            text,
        )
        painter.restore()


#: Wire names for the cue table's columns, in table order. Presence travels by
#: key rather than by column number so that reordering the table here does not
#: silently move everybody else's highlight one column sideways.
CUE_COLUMN_KEYS = ("time", "type", "cue", "label")


class CueTablePresenceOverlay(QWidget):
    """Outlines the cells other machines currently have open in an editor.

    A transparent child of the table's viewport rather than anything the table
    itself knows about: it must never take a click, change the selection, or
    make the table rebuild. It resolves cue ids to rows on each paint, over
    the visible rows only, so a thousand-cue track costs the same as a short
    one.
    """

    def __init__(self, table) -> None:
        super().__init__(table.viewport())
        self._table = table
        #: (cue_id, column_key, colour) for each machine that is typing.
        self._marks: tuple[tuple[str, str, str], ...] = ()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setGeometry(table.viewport().rect())
        table.viewport().installEventFilter(self)
        table.verticalScrollBar().valueChanged.connect(self._scrolled)
        table.horizontalScrollBar().valueChanged.connect(self._scrolled)
        self.hide()

    # -- what the controller hands it ------------------------------------

    def set_marks(self, marks) -> None:
        """Which cells are being edited elsewhere, and in whose colour.

        Repaints only on a real change: this arrives on the network tick
        whether or not anybody started or stopped typing.
        """
        frozen = tuple(
            (
                cue_key(mark.get("cue_id")),
                str(mark.get("column") or ""),
                str(mark.get("colour") or "#4C8DF6"),
            )
            for mark in (marks or ())
            if cue_key(mark.get("cue_id")) and mark.get("column")
        )
        if frozen == self._marks:
            return
        self._marks = frozen
        self.setVisible(bool(frozen))
        if frozen:
            self.raise_()
            self.update()

    # -- staying the right shape -----------------------------------------

    def eventFilter(self, watched, event):
        if watched is self._table.viewport() and event.type() == QEvent.Type.Resize:
            self.setGeometry(self._table.viewport().rect())
        return False

    def _scrolled(self) -> None:
        if self._marks:
            self.update()

    # -- drawing ----------------------------------------------------------

    def _visible_rows(self) -> range:
        """The rows on screen, with one of slack at each end.

        Scrolling moves a row half out of view before it is gone; painting the
        row above and below means a highlight slides off the edge rather than
        vanishing a moment early.
        """
        table = self._table
        first = table.rowAt(0)
        last = table.rowAt(max(0, table.viewport().height() - 1))
        if first < 0:
            first = 0
        if last < 0:
            last = table.rowCount() - 1
        return range(max(0, first - 1), min(table.rowCount(), last + 2))

    def paintEvent(self, event) -> None:
        if not self._marks:
            return

        table = self._table
        rows = self._visible_rows()
        if not rows:
            return

        # cue id -> row, built once per paint from the visible rows only.
        on_screen: dict[str, int] = {}
        for row in rows:
            item = table.item(row, 0)
            if item is None:
                continue
            key = cue_key(item.data(Qt.ItemDataRole.UserRole))
            if key:
                on_screen[key] = row

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        try:
            for cue_id, column_key, colour_hex in self._marks:
                row = on_screen.get(cue_id)
                if row is None:
                    # Their cue is scrolled out of this table, or belongs to a
                    # track this machine does not have open. Saying nothing
                    # beats drawing on the wrong row.
                    continue
                try:
                    column = CUE_COLUMN_KEYS.index(column_key)
                except ValueError:
                    continue
                item = table.item(row, column)
                if item is None:
                    continue
                colour = QColor(colour_hex)
                if not colour.isValid():
                    colour = QColor("#4C8DF6")
                rect = QRectF(table.visualItemRect(item)).adjusted(1.5, 1.5, -1.5, -1.5)
                painter.setPen(QPen(colour, 1.5))
                painter.drawRoundedRect(rect, 5.0, 5.0)
        finally:
            painter.end()
