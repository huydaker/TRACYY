"""The Sync page: hosting or joining a Tracyy Live session, and who is in it.

The page is a view over :class:`tracyy.sync.session.SyncSession` and holds no
state of its own beyond the widgets. Every button ends in a call on the
session, and every redraw starts from what the session says — so when the
network layer lands and starts pushing peers in, the page follows without
being touched.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import time
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from tracyy.core.ui_helpers import silent_message_box
from tracyy.sync.media import MediaDownloader
from tracyy.sync.model import (
    ROLE_LABELS,
    Capability,
    SessionState,
    SyncRole,
    colour_on_dark,
    new_session_code,
)
from tracyy.sync.protocol import SyncError, split_address
from tracyy.sync.session import local_display_name, settings_store
from tracyy.sync.store import human_bytes, live_root, store_for
from tracyy.ui.qr import qr_pixmap
from tracyy.widgets import CUE_COLUMN_KEYS, PLAYLIST_TITLE_ROLE

#: Sessions this machine has joined before, so rejoining tomorrow is one click
#: rather than asking somebody to read the address out again.
_RECENT_KEY = "sync/recent_sessions"
_RECENT_LIMIT = 6

#: What each wire column key is called in the table header, so the caption
#: names the column the way the screen does.
CUE_COLUMN_LABELS = dict(
    zip(CUE_COLUMN_KEYS, ("TIME", "CUE TYPE", "CUE", "LABEL"), strict=True)
)


class SyncControllerMixin:
    # -- page ------------------------------------------------------------

    def build_sync_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("syncPage")
        page_layout = QHBoxLayout(page)
        page_layout.setContentsMargins(26, 22, 26, 22)
        page_layout.setSpacing(0)

        # A settings page reads worse the wider it gets: on a 1500px window the
        # role control ends up a hand's width from the name it belongs to.
        column = QWidget()
        column.setObjectName("syncColumn")
        column.setMaximumWidth(980)
        column.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        page_layout.addWidget(column, 1)
        page_layout.addStretch(0)

        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        heading = QLabel("Tracyy Live")
        heading.setObjectName("syncTitle")
        subheading = QLabel(
            "Kết nối các máy trong cùng mạng LAN và quản lý quyền của người tham gia"
        )
        subheading.setObjectName("syncSubtitle")
        subheading.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(subheading)
        feature_note = QLabel("Đã hỗ trợ kết nối và phân quyền. Đồng bộ nhạc, cue và con trỏ đang phát triển.")
        feature_note.setObjectName("syncCardNote")
        feature_note.setWordWrap(True)
        layout.addWidget(feature_note)
        layout.addSpacing(18)

        # -- your name, which is what everyone else sees -------------------
        identity_row = QHBoxLayout()
        identity_row.setSpacing(10)
        identity_caption = QLabel("TÊN CỦA BẠN")
        identity_caption.setObjectName("syncCaption")
        self.sync_name_edit = QLineEdit(local_display_name())
        self.sync_name_edit.setObjectName("syncNameField")
        self.sync_name_edit.setMaxLength(32)
        self.sync_name_edit.setPlaceholderText("Tên hiện với người khác")
        self.sync_name_edit.setMaximumWidth(280)
        self.sync_name_edit.editingFinished.connect(self.on_sync_name_edited)
        identity_row.addWidget(identity_caption)
        identity_row.addWidget(self.sync_name_edit)
        identity_row.addStretch()
        layout.addLayout(identity_row)
        layout.addSpacing(16)

        # -- the session card swaps between offline and live -------------
        self.sync_session_card = QFrame()
        self.sync_session_card.setObjectName("syncCard")
        self.sync_session_layout = QVBoxLayout(self.sync_session_card)
        self.sync_session_layout.setContentsMargins(18, 16, 18, 18)
        self.sync_session_layout.setSpacing(12)
        layout.addWidget(self.sync_session_card)
        layout.addSpacing(18)

        self.sync_people_caption = QLabel("NGƯỜI ĐANG KẾT NỐI")
        self.sync_people_caption.setObjectName("syncCaption")
        layout.addWidget(self.sync_people_caption)
        layout.addSpacing(8)

        self.sync_people_container = QWidget()
        self.sync_people_container.setObjectName("syncPeopleContainer")
        self.sync_people_layout = QVBoxLayout(self.sync_people_container)
        self.sync_people_layout.setContentsMargins(0, 0, 0, 0)
        self.sync_people_layout.setSpacing(6)
        layout.addWidget(self.sync_people_container)

        layout.addStretch(1)

        self.sync_session.changed.connect(self.rebuild_sync_view)
        self.rebuild_sync_view()
        return page

    # -- redraw ----------------------------------------------------------

    def rebuild_sync_view(self) -> None:
        # The name can change from somewhere other than this field — a rename
        # while hosting, or another window of the app — so the field follows
        # the session rather than only seeding from it once.
        if not self.sync_name_edit.hasFocus():
            current = local_display_name()
            if self.sync_name_edit.text() != current:
                self.sync_name_edit.setText(current)

        self.rebuild_sync_session_card()
        self.rebuild_sync_people()

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # setParent(None) before deleteLater: until the event loop runs
                # the deletion the old widget still has a parent and still
                # paints where it was.
                widget.setParent(None)
                widget.deleteLater()
            else:
                child = item.layout()
                if child is not None:
                    SyncControllerMixin._clear_layout(child)
                    child.deleteLater()

    def rebuild_sync_session_card(self) -> None:
        self._clear_layout(self.sync_session_layout)
        session = self.sync_session

        if session.state != SessionState.OFFLINE:
            self._build_live_session_card()
        else:
            self._build_offline_session_card()

    def _build_offline_session_card(self) -> None:
        layout = self.sync_session_layout

        title = QLabel("Chưa ở trong phiên nào")
        title.setObjectName("syncCardTitle")
        layout.addWidget(title)

        note = QLabel(
            "Tạo phiên để mời máy khác kết nối, hoặc nhập mã của phiên đang chạy."
        )
        note.setObjectName("syncCardNote")
        note.setWordWrap(True)
        layout.addWidget(note)

        host_row = QHBoxLayout()
        host_row.setSpacing(8)
        self.sync_host_name = QLineEdit(self.suggested_sync_session_name())
        self.sync_host_name.setObjectName("syncField")
        self.sync_host_name.setPlaceholderText("Tên phiên")
        host_button = QPushButton("Tạo phiên")
        host_button.setObjectName("syncPrimaryButton")
        host_button.setCursor(Qt.CursorShape.PointingHandCursor)
        host_button.clicked.connect(self.on_sync_host_clicked)
        host_row.addWidget(self.sync_host_name, 1)
        host_row.addWidget(host_button)
        layout.addLayout(host_row)

        network_row = QHBoxLayout()
        network_row.addWidget(QLabel("Mạng máy chính"))
        self.sync_host_network = QComboBox()
        self.sync_host_network.setObjectName("syncField")
        self.sync_host_network.setView(QListView())
        for label, address in self.available_server_networks():
            if address and address != "0.0.0.0":
                self.sync_host_network.addItem(f"{label} · {address}", address)
        if not self.sync_host_network.count():
            self.sync_host_network.addItem("Chỉ máy này · 127.0.0.1", "127.0.0.1")
        network_row.addWidget(self.sync_host_network, 1)
        network_row.addWidget(QLabel("Cổng"))
        self.sync_host_port = QSpinBox()
        self.sync_host_port.setRange(1024, 65535)
        self.sync_host_port.setValue(8090)
        network_row.addWidget(self.sync_host_port)
        layout.addLayout(network_row)

        divider = QFrame()
        divider.setObjectName("syncDivider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        join_row = QHBoxLayout()
        join_row.setSpacing(8)
        self.sync_join_code = QLineEdit()
        self.sync_join_code.setObjectName("syncField")
        self.sync_join_code.setPlaceholderText("Mã phiên")
        self.sync_join_code.setMaximumWidth(130)
        self.sync_join_address = QLineEdit()
        self.sync_join_address.setObjectName("syncField")
        self.sync_join_address.setPlaceholderText("Địa chỉ máy chính, ví dụ 192.168.1.20:8090")
        join_button = QPushButton("Tham gia")
        join_button.setObjectName("syncSecondaryButton")
        join_button.setCursor(Qt.CursorShape.PointingHandCursor)
        join_button.clicked.connect(self.on_sync_join_clicked)
        join_row.addWidget(self.sync_join_code)
        join_row.addWidget(self.sync_join_address, 1)
        join_row.addWidget(join_button)
        layout.addLayout(join_row)

        recent = self.recent_sync_sessions()
        if recent:
            recent_caption = QLabel("PHIÊN GẦN ĐÂY")
            recent_caption.setObjectName("syncCaption")
            layout.addSpacing(4)
            layout.addWidget(recent_caption)

            for entry in recent:
                layout.addWidget(self._build_recent_row(entry))

        layout.addSpacing(4)
        layout.addWidget(self._build_storage_row())

    def _build_recent_row(self, entry: dict) -> QWidget:
        row = QFrame()
        row.setObjectName("syncRecentRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 8, 8, 8)
        row_layout.setSpacing(10)

        name = QLabel(str(entry.get("name", "") or "Phiên không tên"))
        name.setObjectName("syncRecentName")
        address = QLabel(str(entry.get("address", "") or ""))
        address.setObjectName("syncRecentAddress")

        row_layout.addWidget(name)
        row_layout.addWidget(address, 1)

        store = store_for(str(entry.get("show_id", "") or ""))
        downloaded = store.size_bytes() if store is not None else 0

        size_label = QLabel(human_bytes(downloaded) if downloaded else "chưa tải gì")
        size_label.setObjectName("syncRecentSize")
        size_label.setProperty("empty", "false" if downloaded else "true")
        row_layout.addWidget(size_label)

        # Only offered when there is something to delete: a button that does
        # nothing still invites the question "did that work?".
        if downloaded:
            clear = QPushButton("Xoá dữ liệu")
            clear.setObjectName("syncDangerButton")
            clear.setCursor(Qt.CursorShape.PointingHandCursor)
            clear.clicked.connect(
                lambda _checked=False, item=dict(entry): self.on_sync_clear_data(item)
            )
            row_layout.addWidget(clear)

        rejoin = QPushButton("Nối lại")
        rejoin.setObjectName("syncSecondaryButton")
        rejoin.setCursor(Qt.CursorShape.PointingHandCursor)
        rejoin.clicked.connect(
            lambda _checked=False, item=dict(entry): self.on_sync_rejoin_clicked(item)
        )
        row_layout.addWidget(rejoin)
        return row

    def _build_storage_row(self) -> QWidget:
        """Where downloaded music and cues live, and a way to get there.

        Shown as a path rather than described, because the question this
        answers is "my disk is full, what is Tracyy keeping and where" — and
        that question is only answerable with the actual path.
        """
        row = QFrame()
        row.setObjectName("syncStorageRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 8, 8, 8)
        row_layout.setSpacing(10)

        caption = QLabel("NHẠC TẢI VỀ")
        caption.setObjectName("syncCaption")

        path_label = QLabel(str(live_root()))
        path_label.setObjectName("syncStoragePath")
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        total = sum(
            (store.size_bytes() if store is not None else 0)
            for store in (
                store_for(str(item.get("show_id", "") or ""))
                for item in self.recent_sync_sessions()
            )
        )
        total_label = QLabel(human_bytes(total) if total else "trống")
        total_label.setObjectName("syncRecentSize")
        total_label.setProperty("empty", "false" if total else "true")

        open_button = QPushButton("Mở thư mục")
        open_button.setObjectName("syncSecondaryButton")
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.clicked.connect(self.on_sync_open_storage)

        row_layout.addWidget(caption)
        row_layout.addWidget(path_label, 1)
        row_layout.addWidget(total_label)
        row_layout.addWidget(open_button)
        return row

    def _build_live_session_card(self) -> None:
        layout = self.sync_session_layout
        session = self.sync_session
        hosting = session.state == SessionState.HOSTING
        disconnected = session.state == SessionState.DISCONNECTED

        header = QHBoxLayout()
        header.setSpacing(10)

        dot = QLabel()
        dot.setObjectName("syncLiveDot")
        dot.setProperty("connecting", "true" if session.state == SessionState.CONNECTING else "false")
        dot.setProperty("disconnected", "true" if disconnected else "false")
        dot.setFixedSize(9, 9)

        title = QLabel(session.session_name or "Phiên không tên")
        title.setObjectName("syncCardTitle")

        state_pill = QLabel(
            "ĐANG CHỦ TRÌ" if hosting
            else ("MẤT KẾT NỐI" if disconnected
            else ("ĐANG NỐI…" if session.state == SessionState.CONNECTING else "ĐÃ THAM GIA")
            )
        )
        state_pill.setObjectName("syncStatePill")
        state_pill.setProperty("hosting", "true" if hosting else "false")
        state_pill.setProperty("disconnected", "true" if disconnected else "false")

        leave = QPushButton("Rời phiên")
        leave.setObjectName("syncDangerButton")
        leave.setCursor(Qt.CursorShape.PointingHandCursor)
        leave.clicked.connect(self.on_sync_leave_clicked)

        header.addWidget(dot)
        header.addWidget(title, 1)
        header.addWidget(state_pill)
        header.addWidget(leave)
        layout.addLayout(header)

        if disconnected:
            reply = self.live_client.snapshot()
            note = QLabel("Cue đang bị khoá. " + str(reply.get("error") or "Đang thử nối lại máy chính."))
            note.setWordWrap(True)
            note.setObjectName("syncCardNote")
            layout.addWidget(note)
            retry = QPushButton("Thử nối lại")
            retry.setObjectName("syncSecondaryButton")
            retry.clicked.connect(lambda: self.begin_sync_join(
                session.host_address, session.session_code, session.session_name,
            ))
            layout.addWidget(retry)

        progress = self.media_progress_text()
        if progress:
            self.sync_media_label = QLabel(progress)
            self.sync_media_label.setObjectName("syncCardNote")
            self.sync_media_label.setWordWrap(True)
            layout.addWidget(self.sync_media_label)

        body = QHBoxLayout()
        body.setSpacing(16)

        details = QVBoxLayout()
        details.setSpacing(6)

        code_caption = QLabel("MÃ PHIÊN")
        code_caption.setObjectName("syncCaption")
        code_value = QLabel(session.session_code or "—")
        code_value.setObjectName("syncCodeValue")
        code_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        address_caption = QLabel("ĐỊA CHỈ")
        address_caption.setObjectName("syncCaption")
        address_value = QLabel(session.host_address or "—")
        address_value.setObjectName("syncAddressValue")
        address_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        copy_button = QPushButton("Chép mã và địa chỉ")
        copy_button.setObjectName("syncSecondaryButton")
        copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_button.clicked.connect(self.on_sync_copy_clicked)

        details.addWidget(code_caption)
        details.addWidget(code_value)
        details.addSpacing(4)
        details.addWidget(address_caption)
        details.addWidget(address_value)
        details.addSpacing(6)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        action_row.addWidget(copy_button)
        action_row.addStretch()
        details.addLayout(action_row)

        body.addLayout(details, 1)
        body.addWidget(self._build_sync_qr(session.host_address, session.session_code))
        layout.addLayout(body)

    def _build_sync_qr(self, address: str, code: str) -> QWidget:
        """The join address as a QR, so nobody types an IP into a laptop.

        Only when there is an address to encode: a code that resolves to
        nothing is worse than no code, because the person who scanned it
        cannot tell a broken session from a wrong address.
        """
        holder = QLabel()
        holder.setObjectName("syncQr")
        holder.setFixedSize(104, 104)
        holder.setAlignment(Qt.AlignmentFlag.AlignCenter)

        text = str(address or "").strip()
        if not text:
            holder.setProperty("state", "idle")
            holder.setText("QR hiện\nkhi có\nđịa chỉ")
            return holder

        try:
            ratio = max(1.0, float(self.devicePixelRatioF()))
        except Exception:
            ratio = 2.0

        pixmap = qr_pixmap(f"tracyy://{text}/{code}", 96, ratio=ratio)
        if pixmap is None:
            holder.setProperty("state", "idle")
            holder.setText("QR không\nkhả dụng")
            return holder

        holder.setProperty("state", "live")
        holder.setPixmap(pixmap)
        return holder

    def rebuild_sync_people(self) -> None:
        self._clear_layout(self.sync_people_layout)
        session = self.sync_session

        if not session.is_live and session.state != SessionState.CONNECTING:
            self.sync_people_caption.setText("NGƯỜI ĐANG KẾT NỐI")
            empty = QLabel("Chưa xác nhận được ai đang kết nối." if session.state == SessionState.DISCONNECTED
                           else "Chưa có phiên nào đang chạy.")
            empty.setObjectName("syncEmptyNote")
            self.sync_people_layout.addWidget(empty)
            return

        peers = session.peers()
        self.sync_people_caption.setText(f"NGƯỜI ĐANG KẾT NỐI — {len(peers)}")

        can_manage = session.local_can(Capability.MANAGE_PEOPLE)
        for peer in peers:
            self.sync_people_layout.addWidget(self._build_peer_row(peer, can_manage))

    def _build_peer_row(self, peer, can_manage: bool) -> QWidget:
        row = QFrame()
        row.setObjectName("syncPeerRow")
        row.setProperty("local", "true" if peer.is_local else "false")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 10, 12, 10)
        row_layout.setSpacing(11)

        swatch = QLabel()
        swatch.setObjectName("syncPeerSwatch")
        swatch.setFixedSize(14, 14)
        swatch.setPixmap(self._sync_colour_dot(peer.colour))

        name = QLabel(peer.display_name + ("  (bạn)" if peer.is_local else ""))
        name.setObjectName("syncPeerName")

        row_layout.addWidget(swatch)
        row_layout.addWidget(name, 1)

        if peer.track_name:
            track = QLabel(peer.track_name)
            track.setObjectName("syncPeerTrack")
            row_layout.addWidget(track)

        # The owner's own role is fixed while hosting, so it reads as a label
        # rather than a control that refuses every change.
        owner_locked = peer.role == SyncRole.OWNER
        if can_manage and not owner_locked:
            combo = QComboBox()
            combo.setObjectName("syncRoleCombo")
            combo.setView(QListView())
            for role in (SyncRole.EDITOR, SyncRole.CUE_ONLY, SyncRole.VIEWER):
                combo.addItem(ROLE_LABELS[role], role.value)
            combo.setCurrentIndex(max(0, combo.findData(peer.role.value)))
            combo.currentIndexChanged.connect(
                lambda _index, machine_id=peer.machine_id, box=combo: (
                    self.on_sync_role_changed(machine_id, str(box.currentData() or ""))
                )
            )
            row_layout.addWidget(combo)
        else:
            role_pill = QLabel(ROLE_LABELS.get(peer.role, "—"))
            role_pill.setObjectName("syncRolePill")
            role_pill.setProperty("role", peer.role.value)
            row_layout.addWidget(role_pill)

        return row

    def _sync_colour_dot(self, colour: str) -> QPixmap:
        try:
            ratio = max(1.0, float(self.devicePixelRatioF()))
        except Exception:
            ratio = 2.0
        pixmap = QPixmap(round(14 * ratio), round(14 * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Logical units: the pixmap already carries the ratio, so scaling
            # by the device size here would apply it twice.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colour))
            painter.drawEllipse(1.0, 1.0, 12.0, 12.0)
        finally:
            painter.end()
        return pixmap

    # -- actions ----------------------------------------------------------

    def suggested_sync_session_name(self) -> str:
        current = str(getattr(self, "_current_project_path", "") or "").strip()
        if current:
            from pathlib import Path

            return Path(current).stem
        return "Phiên dựng cue"

    def local_sync_address(self) -> str:
        """The address other machines should type, best guess.

        Reuses the interface list the Server page already builds so the two
        pages can never disagree about which network this machine is on.
        """
        if hasattr(self, "sync_host_network"):
            return f"{self.sync_host_network.currentData()}:{self.sync_host_port.value()}"
        try:
            networks = self.available_server_networks()
        except Exception:
            networks = []
        for _label, address in networks:
            if address and address != "0.0.0.0":
                return f"{address}:8090"
        return "127.0.0.1:8090"

    def on_sync_name_edited(self) -> None:
        cleaned = self.sync_session.rename_local(self.sync_name_edit.text())
        if cleaned != self.sync_name_edit.text():
            self.sync_name_edit.setText(cleaned)

    def on_sync_host_clicked(self) -> None:
        if self.sync_session.state != SessionState.OFFLINE:
            return
        name = self.sync_host_name.text() if hasattr(self, "sync_host_name") else ""
        address = self.local_sync_address()
        code = new_session_code()

        # The socket opens before the session says it is hosting. Announcing a
        # code and an address that nothing is listening on is exactly what made
        # the page look like it worked when there was no network layer at all.
        try:
            self.live_server.start(
                "0.0.0.0",
                split_address(address)[1],
                " ".join(str(name or "").split()) or "Phiên không tên",
                code,
                self.sync_session.machine_id,
                local_display_name(),
                show_id=self.stable_show_id(),
            )
        except SyncError as exc:
            self.show_error(str(exc))
            return

        # The show that already exists goes into the document before anybody
        # can join, or every edit to it is refused as a cue that never was.
        self.live_server.seed_document(self.cue_document_for_session())
        self.sync_session.start_hosting(name, address, code=code)
        self.set_status(f"TRACYY LIVE — đang chủ trì {self.sync_session.session_name}")

    def on_sync_join_clicked(self) -> None:
        code = self.sync_join_code.text().strip().upper()
        address = self.sync_join_address.text().strip()
        if not address:
            self.show_error("Nhập địa chỉ máy chính trước khi tham gia.")
            return
        self.begin_sync_join(address, code, "")

    def on_sync_rejoin_clicked(self, entry: dict) -> None:
        self.begin_sync_join(
            str(entry.get("address", "") or ""),
            str(entry.get("code", "") or ""),
            str(entry.get("name", "") or ""),
        )

    def begin_sync_join(self, address: str, code: str, name: str) -> None:
        """Validate locally; every network request runs off the GUI thread."""
        if self.sync_session.state in {SessionState.HOSTING, SessionState.JOINED}:
            self.set_status("Rời phiên hiện tại trước khi tham gia phiên khác.")
            return
        try:
            split_address(address)
            self.live_client.start(address, code, self.sync_session.machine_id, local_display_name())
        except SyncError as exc:
            self.show_error(str(exc))
            return
        self.sync_session.join(name, code, address)
        self.set_status(f"TRACYY LIVE — đang nối tới {address}")

    def on_sync_leave_clicked(self) -> None:
        self.live_client.stop()
        self.live_server.stop()
        self.sync_session.leave()
        self.set_status("TRACYY LIVE — đã rời phiên")

    def shutdown_sync(self) -> None:
        self.shutdown_media_downloader()
        """Close both ends. Called when the window closes."""
        try:
            self.live_client.stop()
        finally:
            self.live_server.stop()

    # -- the tick that moves the network into the session -----------------

    def local_presence_track(self) -> tuple[str, str]:
        """The open track as ``(id, display name)`` for the presence layer.

        The id is what the other machines match on; the name exists only for
        the ones that cannot resolve it — a peer may have opened a song this
        machine has never seen, and an empty row helps nobody.
        """
        path = str(self.main_track_path() or "")
        if not path:
            return "", ""
        return self.track_id_for_path(path), path.replace("\\", "/").rsplit("/", 1)[-1]

    def tick_sync(self) -> None:
        """Copy what the network knows into the session, when it changed.

        Only when it changed: the session emits one signal for any change and
        the page rebuilds itself from it, so applying an identical peer list
        every tick would destroy the name field several times a second while
        somebody was typing in it.
        """
        session = self.sync_session
        if session.state == SessionState.OFFLINE:
            # Leaving a session has to take the other machines off the
            # waveform with it. Nothing else clears them: the widget keeps
            # what it was last handed.
            self.refresh_presence_overlay()
            return

        if session.state == SessionState.HOSTING:
            track_id, track_name = self.local_presence_track()
            editing_cue_id, editing_column = self.local_editing_cell
            self.live_server.set_host_presence(
                local_display_name(),
                track_id,
                track_name,
                float(self.transport.current_position() or 0.0),
                editing_cue_id,
                editing_column,
            )
            self.live_server.publish_media(self.session_media_tracks())
            self.drain_host_cue_ops()
            peers = self.live_server.snapshot()
        else:
            track_id, track_name = self.local_presence_track()
            editing_cue_id, editing_column = self.local_editing_cell
            self.live_client.set_presence(
                track_id,
                track_name,
                float(self.transport.current_position() or 0.0),
                display_name=local_display_name(),
                editing_cue_id=editing_cue_id,
                editing_column=editing_column,
            )
            reply = self.live_client.snapshot()
            self._apply_client_state(reply)
            self.drain_remote_cue_ops()
            self.collect_downloaded_media(reply)
            self.refresh_presence_overlay()
            return
        session.apply_network_peers(peers)
        self.refresh_presence_overlay()

    def stable_show_id(self) -> str:
        """A folder name for this show that outlives the app being restarted.

        Derived from the project file rather than minted per hosting run: the
        clients name their download folder after it, and a fresh id every time
        somebody reopens Tracyy means every laptop in the room fetches the
        whole show again. Hashed rather than used directly — it is a folder
        name on somebody else's machine, not a tour of this one's disk.
        """
        project = str(getattr(self, "_current_project_path", "") or "")
        if not project:
            return ""
        digest = hashlib.sha256(os.path.normcase(project).encode("utf-8", "replace"))
        return digest.hexdigest()[:16]

    def session_media_tracks(self) -> dict[str, str]:
        """Every song in the playlist that is actually on this disk.

        Missing files are left out rather than offered and then refused: a
        client that asks for one would retry it for the rest of the show.
        """
        tracks: dict[str, str] = {}
        for row in range(self.playlist.count()):
            item = self.playlist.item(row)
            if item is None:
                continue
            path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not path or not os.path.isfile(path):
                continue
            track_id = self.track_id_for_path(path)
            if track_id:
                tracks[track_id] = path
        return tracks

    def media_downloader(self, show_id: str) -> MediaDownloader | None:
        """The downloader for this show, started on first use.

        Keyed by show so that leaving one session and joining another does not
        keep writing into the folder of the show that ended.
        """
        if not show_id:
            return None
        store = store_for(show_id)
        if store is None:
            return None

        existing = getattr(self, "_media_downloader", None)
        if existing is not None and getattr(self, "_media_show_id", "") == show_id:
            return existing
        if existing is not None:
            existing.stop()

        store.ensure()
        downloader = MediaDownloader(
            store.media_dir,
            self.live_client.fetch_media_chunk,
            manifest_fetch=self.live_client.fetch_manifest,
        )
        self._media_downloader = downloader
        self._media_show_id = show_id
        self._media_names = {}
        downloader.start()
        downloader.refresh_manifest()
        return downloader

    def collect_downloaded_media(self, reply: dict) -> None:
        """Take delivery of anything that finished, and put the cues on it.

        A track only becomes real to the rest of the application here: the id
        the session uses is bound to this machine's own copy, and the cue
        operations that were held back because the song was missing are
        replayed against it.
        """
        downloader = self.media_downloader(str(reply.get("show_id") or ""))
        if downloader is None:
            return

        # Names before anything reads them: the progress line is built out of
        # this, and a card drawn first would say t-song-0 where the song's
        # name belongs.
        names = getattr(self, "_media_names", None)
        if names is None:
            names = {}
            self._media_names = names
        for entry in downloader.known_entries():
            names[entry.track_id] = entry.name

        was_busy = bool(getattr(self, "_media_busy", False))
        state = downloader.snapshot()
        busy = bool(state.get("active") or state.get("failed"))
        self._media_busy = busy
        if busy != was_busy:
            # The card only carries the progress line while there is one, so
            # it has to be rebuilt when that changes — not on every beat,
            # which would destroy the name field somebody is typing in.
            self.rebuild_sync_session_card()
        elif busy and getattr(self, "sync_media_label", None) is not None:
            try:
                self.sync_media_label.setText(self.media_progress_text())
            except RuntimeError:
                self.sync_media_label = None

        # The host can add a song after everybody has joined. Asking again on
        # a slow cadence rather than every beat: the answer costs the host a
        # hash of anything it has not hashed yet, and nothing in a show
        # changes the running order thirty times a minute.
        now = time.monotonic()
        if now - float(getattr(self, "_media_manifest_asked", 0.0)) > 30.0:
            self._media_manifest_asked = now
            downloader.refresh_manifest()

        landed = downloader.take_completed()
        if not landed:
            return

        for track_id, path in landed.items():
            self.adopt_track_id(path, track_id)
            self.cues_by_track.setdefault(path, [])
        # Into the playlist, or the song is on the disk and nowhere a person
        # can see it. add_paths skips anything already there, so a re-join
        # does not fill the list with duplicates.
        self.add_paths(sorted(landed.values()))
        # On disk the file carries its track id so two songs with one name
        # cannot land on each other; in the playlist that is noise, so the row
        # shows what the song is actually called.
        by_path = {path: track_id for track_id, path in landed.items()}
        for row in range(self.playlist.count()):
            item = self.playlist.item(row)
            if item is None:
                continue
            track_id = by_path.get(str(item.data(Qt.ItemDataRole.UserRole) or ""))
            title = names.get(track_id or "", "")
            if title:
                item.setData(PLAYLIST_TITLE_ROLE, Path(title).stem)

        held = self.deferred_cue_ops()
        if held:
            # Everything, not just this track's: replaying the whole pile is
            # cheap at human speed, and anything still homeless simply goes
            # back on it.
            pending, held[:] = list(held), []
            self.apply_remote_cue_ops(pending)

        names = ", ".join(sorted(Path(path).name for path in landed.values()))
        self.set_status(f"TRACYY LIVE — đã tải xong {names}")

    def media_progress_text(self) -> str:
        """What the download queue is doing, in one line, or nothing.

        Nothing while there is nothing to say: a row that reads "0 bài đang
        tải" is a row that takes up space in every session that never needed
        one.
        """
        downloader = getattr(self, "_media_downloader", None)
        if downloader is None:
            return ""

        state = downloader.snapshot()
        failed = state.get("failed") or {}
        if state.get("active"):
            size = max(1, int(state.get("size") or 0))
            done = int(state.get("received") or 0)
            names = getattr(self, "_media_names", {})
            name = str(names.get(state["active"], state["active"]))
            waiting = max(0, int(state.get("waiting") or 1) - 1)
            queued = f"  ·  còn {waiting} bài" if waiting else ""
            return (
                f"Đang tải {name} — {done * 100 // size}% "
                f"({human_bytes(done)} / {human_bytes(size)}){queued}"
            )
        if failed:
            return "Không tải được: " + ", ".join(
                str(getattr(self, "_media_names", {}).get(track_id, track_id))
                for track_id in failed
            )
        return ""

    def shutdown_media_downloader(self) -> None:
        downloader = getattr(self, "_media_downloader", None)
        if downloader is not None:
            downloader.stop()
        self._media_downloader = None
        self._media_show_id = ""

    def commit_cue_operation(self, kind: str, cue_id: str, payload: dict) -> bool:
        """Put one cue change through the session before it happens here.

        True means go ahead: either this machine is working alone, or the host
        has written the change down. False means change nothing locally — the
        entire point is that the two never end up disagreeing about what the
        show contains.

        Submitting first and applying after is deliberate. Applying first
        would be faster to look at and impossible to undo cleanly the moment
        the host says no.
        """
        session = self.sync_session
        if session.state == SessionState.OFFLINE:
            return True

        track_id, _track_name = self.local_presence_track()
        if not track_id:
            return True

        op = {
            "op_id": uuid.uuid4().hex,
            "track_id": track_id,
            "cue_id": str(cue_id),
            "kind": str(kind),
            "payload": dict(payload),
        }

        if session.state == SessionState.HOSTING:
            reply = self.live_server.commit_local(op)
        else:
            reply = self.live_client.submit_operation(op)

        if reply.get("ok"):
            return True

        self.set_status(
            "TRACYY LIVE — "
            + str(reply.get("error") or "Máy chính không nhận thao tác này.")
        )
        return False

    def catch_up_on_cue_document(self) -> None:
        """Take the host's cue sheet in one go, on joining.

        Once per join. The operations that follow are the changes made from
        here on; this is everything that was already there.
        """
        snapshot = self.live_client.fetch_document()
        if not snapshot:
            return
        self.apply_cue_snapshot(snapshot)
        try:
            revision = int(snapshot.get("revision", 0))
        except (TypeError, ValueError):
            return
        # Acknowledged only now, and only up to what the snapshot contained:
        # anything committed while it was in flight still arrives as an
        # operation on the next beat.
        self.live_client.confirm_applied(revision)

    def drain_host_cue_ops(self) -> None:
        """Show, on the hosting machine, what the other machines committed.

        The host owns the document, which is exactly why this is easy to
        forget: the change is already saved, already journalled, already on
        its way to everybody else, and the one screen that does not have it is
        the one belonging to the person running the show.
        """
        records = self.live_server.take_host_ops()
        if records:
            self.apply_remote_cue_ops(records)

    def drain_remote_cue_ops(self) -> None:
        """Apply whatever the host has committed since the last beat.

        The acknowledgement comes last on purpose. Saying "I have revision N"
        before the cue table really holds it would tell the host to stop
        sending exactly the records that went missing, and nothing would ever
        notice.
        """
        records = self.live_client.take_ops()
        if not records:
            return
        self.apply_remote_cue_ops(records)
        self.live_client.confirm_applied(records[-1]["revision"])

    def refresh_presence_overlay(self) -> None:
        """Hand the main waveform everyone else's position on this track.

        Only the people on the same song, and only as a fraction of its
        length: the widget knows nothing about sessions, seconds or peers, and
        should not start now. Somebody on another track is left to the row
        above the waveform — drawing them here would put a mark on a song they
        are not looking at.

        Duration comes from the transport rather than the peer, so a machine
        whose copy of the file differs in length cannot push a mark past the
        end of this one.
        """
        waveform = getattr(self, "main_waveform", None)
        if waveform is None:
            return

        marks = []
        track_id, _track_name = self.local_presence_track()
        duration = float(getattr(self.transport, "duration", 0.0) or 0.0)
        if track_id and duration > 0.0:
            for peer in self.sync_session.peers_on_track(track_id):
                marks.append(
                    {
                        "id": peer.machine_id,
                        "colour": colour_on_dark(peer.colour),
                        "name": peer.display_name,
                        "position": max(
                            0.0, min(1.0, float(peer.position_seconds) / duration)
                        ),
                    }
                )
        waveform.set_peer_presence(marks, duration=duration)
        self.refresh_cue_presence_overlay(track_id)

    def refresh_cue_presence_overlay(self, track_id: str) -> None:
        """Outline the cells the other machines are typing in.

        Only for people on this song. Somebody editing a cue on another track
        has a cue id that is not in this table, and the overlay would find no
        row for it anyway — but filtering here keeps a stale id from ever
        matching a cue that happens to share it.
        """
        overlay = getattr(self, "cue_presence_overlay", None)
        if overlay is None:
            return

        marks = []
        captions = []
        if track_id:
            for peer in self.sync_session.peers_editing(track_id):
                colour = colour_on_dark(peer.colour)
                marks.append(
                    {
                        "cue_id": peer.editing_cue_id,
                        "column": peer.editing_column,
                        "colour": colour,
                    }
                )
                captions.append(self._editing_caption(peer, colour))
        overlay.set_marks(marks)
        self._set_cue_presence_status(captions)

    def _editing_caption(self, peer, colour: str) -> str:
        """One person's "editing X of Y", in their colour."""
        cue = self.find_cue(peer.editing_cue_id)
        cue_name = str(cue.get("name") or "") if cue else ""
        column = CUE_COLUMN_LABELS.get(peer.editing_column, peer.editing_column.upper())
        where = f"{column} · {cue_name}" if cue_name else column
        name = html.escape(peer.display_name)
        return f'<span style="color:{colour}">{name} đang sửa {html.escape(where)}</span>'

    def _set_cue_presence_status(self, captions: list[str]) -> None:
        label = getattr(self, "cue_presence_status", None)
        if label is None:
            return
        text = "&nbsp;&nbsp;·&nbsp;&nbsp;".join(captions)
        if text == label.text():
            return
        label.setText(text)
        label.setVisible(bool(text))

    def _apply_sync_presence(self, peers: list[dict]) -> None:
        """Positions only. Never emits a change, so nothing redraws."""
        session = self.sync_session
        for raw in peers:
            session.update_presence(
                str(raw.get("machine_id", "")),
                track_id=str(raw.get("track_id", "") or ""),
                track_name=str(raw.get("track_name", "") or ""),
                position_seconds=raw.get("position_seconds", 0.0),
            )

    def _apply_client_state(self, reply: dict) -> None:
        state = str(reply.get("state", ""))
        session = self.sync_session

        if state == "joined":
            was_joined = session.state == SessionState.JOINED
            session.apply_network_peers(reply.get("peers", []), str(reply.get("session_name") or ""))
            session.mark_joined()
            if not was_joined:
                self.remember_sync_session(session.session_name, session.host_address, session.session_code)
                self.set_status(f"TRACYY LIVE — đã tham gia {session.session_name}")
                self.catch_up_on_cue_document()

        elif state in {"disconnected", "refused"}:
            message = str(reply.get("error", "") or "")
            if session.state != SessionState.DISCONNECTED:
                session.mark_disconnected()
                session.apply_network_peers([])
            elif message != getattr(self, "_sync_connection_error", ""):
                session.changed.emit()
            if message and message != getattr(self, "_sync_connection_error", ""):
                self.set_status(f"TRACYY LIVE — {message}")
            self._sync_connection_error = message

    def on_sync_copy_clicked(self) -> None:
        session = self.sync_session
        QApplication.clipboard().setText(
            f"{session.host_address}  ·  mã {session.session_code}"
        )
        self.set_status("ĐÃ CHÉP — mã và địa chỉ phiên")

    def on_sync_role_changed(self, machine_id: str, role: str) -> None:
        # The server is what the other machine asks; changing only the local
        # list would show a role here that never reached the laptop it is for.
        if self.sync_session.state != SessionState.HOSTING:
            return
        if self.live_server.running and self.live_server.set_role(machine_id, role):
            self.sync_session.apply_network_peers(self.live_server.snapshot())
            peer = self.sync_session.peer(machine_id)
            if peer is not None:
                self.set_status(f"QUYỀN — {peer.display_name} · {ROLE_LABELS[peer.role]}")

    def on_sync_clear_data(self, entry: dict) -> None:
        """Delete one show's downloaded music and cues from this machine."""
        store = store_for(str(entry.get("show_id", "") or ""))
        if store is None:
            self.show_error("Phiên này chưa có thư mục dữ liệu.")
            return

        size = store.size_bytes()
        name = str(entry.get("name", "") or "phiên này")
        answer = silent_message_box(
            self,
            "Xoá dữ liệu đã tải",
            f"Xoá {human_bytes(size)} nhạc và cue đã tải về cho “{name}”?\n\n"
            "Chỉ xoá bản sao trên máy này. Bản gốc trên máy chính không đổi, "
            "và lần sau nối lại sẽ tải lại.",
            buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            default_button=QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        if store.delete():
            self.set_status(f"ĐÃ XOÁ — {human_bytes(size)} dữ liệu của {name}")
        else:
            self.show_error("Không xoá được thư mục dữ liệu của phiên này.")
        self.rebuild_sync_view()

    def on_sync_open_storage(self) -> None:
        root = live_root()
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    # -- recent sessions ---------------------------------------------------

    def recent_sync_sessions(self) -> list[dict]:
        raw = settings_store().value(_RECENT_KEY, "", type=str)
        try:
            entries = json.loads(str(raw or "[]"))
        except (ValueError, TypeError):
            return []
        return [entry for entry in entries if isinstance(entry, dict)][:_RECENT_LIMIT]

    def remember_sync_session(self, name: str, address: str, code: str) -> None:
        address = str(address or "").strip()
        if not address:
            return

        entries = [
            entry
            for entry in self.recent_sync_sessions()
            if str(entry.get("address", "")) != address
        ]
        # A show id rather than the session code: the code is regenerated every
        # time somebody hosts, so keying the downloaded data on it would orphan
        # a few gigabytes every morning. Replaced by the host's own id once
        # there is a host to ask.
        previous = next(
            (
                str(item.get("show_id", ""))
                for item in self.recent_sync_sessions()
                if str(item.get("address", "")) == address and item.get("show_id")
            ),
            "",
        )
        entries.insert(
            0,
            {
                "name": str(name or "").strip() or address,
                "address": address,
                "code": str(code or "").strip().upper(),
                "show_id": previous or uuid.uuid4().hex[:12],
            },
        )
        settings_store().setValue(
            _RECENT_KEY,
            json.dumps(entries[:_RECENT_LIMIT], ensure_ascii=False),
        )

    # -- what the rest of the application asks -----------------------------

    def require_cue_edit_rights(self) -> bool:
        """Whether cues may be changed right now, saying why when they may not.

        Checked at the top of every cue mutation rather than by greying out
        controls: the cue table, the waveform drag and the keyboard shortcuts
        all reach the same operations by different routes, and a disabled
        button only closes one of the three.
        """
        session = getattr(self, "sync_session", None)
        if session is not None and session.state in {SessionState.JOINED, SessionState.CONNECTING}:
            self._apply_client_state(self.live_client.snapshot())
        if session is None or session.local_can(Capability.EDIT_CUES):
            return True

        if session.state == SessionState.DISCONNECTED:
            self.set_status("MẤT KẾT NỐI — cue bị khoá cho tới khi nối lại")
        else:
            self.set_status("KHÔNG CÓ QUYỀN SỬA CUE — hỏi chủ phiên để được cấp")
        return False

    def sync_allows(self, capability: Capability | str) -> bool:
        """One place for "am I allowed to do this in the current session".

        Returns True when not in a session at all, so a machine working alone
        is never restricted by a session it has left.
        """
        session = getattr(self, "sync_session", None)
        if session is None:
            return True
        return session.local_can(capability)
