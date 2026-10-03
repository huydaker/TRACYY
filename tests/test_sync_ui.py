"""Real Sync widgets/controller, isolated from audio devices and licences."""

from __future__ import annotations

import os
import socket
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QListWidget,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tracyy.controllers import sync as controller_module
from tracyy.controllers.cue import CueControllerMixin
from tracyy.controllers.sync import SyncControllerMixin
from tracyy.sync import client as client_module
from tracyy.sync import session as session_module
from tracyy.sync import store as store_module
from tracyy.sync.client import LiveClient
from tracyy.sync.model import Capability, SessionState
from tracyy.sync.server import LiveServer
from tracyy.sync.session import SyncSession
from tracyy.ui.theme import apply_theme


class SyncWindow(SyncControllerMixin, CueControllerMixin, QWidget):
    def __init__(self):
        super().__init__()
        self.sync_session = SyncSession(self)
        self.live_server = LiveServer()
        self.live_client = LiveClient()
        self.transport = SimpleNamespace(current_position=lambda: 12.0)
        self.statuses, self.errors = [], []
        self._undo_stack = [{"description": "Must stay untouched"}]
        self.cue_type_defs = [("Light", "#ffffff")]
        self.cue_type_meta = {"Light": {"locked": False}}
        self.active_cue_type = "Light"
        self.cues_by_track = {"local.wav": []}
        # Hosting offers the playlist's music to the session, so the shell
        # needs one even though these tests never put a song in it.
        self.playlist = QListWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self.build_sync_page())
        self.resize(1100, 820)

    def available_server_networks(self):
        return [("Auto", "0.0.0.0"), ("LAN", "192.168.1.20"), ("Local", "127.0.0.1")]

    def main_track_path(self):
        return "local.wav"

    def track_id_for_path(self, path):
        return f"t-{path}"

    #: Normally supplied by the cue-follow controller, which this shell does
    #: not mix in. tick_sync reads it every beat to tell the other machines
    #: which cell is being typed in.
    local_editing_cell = ("", "")

    def set_status(self, message):
        self.statuses.append(message)

    def show_error(self, message):
        self.errors.append(message)


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    return app


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    def settings():
        return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)

    monkeypatch.setattr(session_module, "settings_store", settings)
    monkeypatch.setattr(controller_module, "settings_store", settings)
    monkeypatch.setattr(controller_module, "live_root", lambda: tmp_path / "live")
    monkeypatch.setattr(store_module, "live_root", lambda: tmp_path / "live")
    window = SyncWindow()
    window.show()
    app.processEvents()
    try:
        yield window
    finally:
        window.shutdown_sync()
        wait_until(app, lambda: not window.live_client.running)
        window.close()
        window.deleteLater()
        app.processEvents()


def wait_until(app, check, tick=None, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if tick:
            tick()
        if check():
            return
        time.sleep(0.025)
    raise AssertionError("UI did not receive the expected state")


def labels(window):
    return [label.text() for label in window.findChildren(QLabel)]


def capture(window, app, path):
    # Newly added children become visible on the next layout/show event.
    app.processEvents()
    app.processEvents()
    title = window.findChild(QLabel, "syncCardTitle")
    assert title is not None and title.isVisible()
    assert window.grab().save(str(path))


def test_host_ui_uses_selected_address_and_only_grants_client_roles(window, app, tmp_path):
    window.sync_host_network.setCurrentIndex(1)
    assert window.local_sync_address().startswith("127.0.0.1:")
    capture(window, app, tmp_path / "sync-offline.png")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        window.sync_host_port.setValue(probe.getsockname()[1])
    window.sync_host_name.setText("Dựng cue")
    window.on_sync_host_clicked()
    assert window.sync_session.state == SessionState.HOSTING, window.errors
    assert window.live_server.running
    remote = LiveClient()
    try:
        remote.start(
            window.sync_session.host_address,
            window.sync_session.session_code,
            "remote",
            "Máy Windows",
        )
        wait_until(app, lambda: len(window.sync_session.peers()) == 2, window.tick_sync)
        assert "Máy Windows" in labels(window)
        assert "NGƯỜI ĐANG KẾT NỐI — 2" in labels(window)
        window.on_sync_role_changed("remote", "editor")
        wait_until(app, lambda: remote.snapshot()["you"].get("role") == "editor")
        window.on_sync_role_changed(window.sync_session.machine_id, "viewer")
        assert window.sync_session.local_role.value == "owner"
        capture(window, app, tmp_path / "sync-host.png")
    finally:
        remote.stop()
        wait_until(app, lambda: not remote.running)


def test_join_is_nonblocking_and_disconnect_locks_actual_cue_actions(
    window, app, monkeypatch, tmp_path
):
    host = LiveServer()
    host.start("127.0.0.1", 0, "Chuẩn bị • Tour", "ABC234", "host", "Máy chính")
    release, entered = threading.Event(), threading.Event()
    real_post = client_module._post

    def delayed(base, path, payload, **kwargs):
        entered.set()
        assert release.wait(6)
        return real_post(base, path, payload, **kwargs)

    monkeypatch.setattr(client_module, "_post", delayed)
    try:
        started = time.monotonic()
        window.begin_sync_join(f"127.0.0.1:{host.port}", "ABC234", "")
        assert time.monotonic() - started < 0.5
        assert entered.wait(2)
        assert window.sync_session.state == SessionState.CONNECTING
        assert not window.require_cue_edit_rights()
        release.set()
        monkeypatch.setattr(client_module, "_post", real_post)
        wait_until(
            app, lambda: window.sync_session.state == SessionState.JOINED, window.tick_sync
        )
        assert window.sync_session.session_name == "Chuẩn bị • Tour"
        assert "Máy chính" in labels(window)
        assert not window.require_cue_edit_rights()
        host.set_role(window.sync_session.machine_id, "editor")
        wait_until(
            app, lambda: window.sync_session.local_can(Capability.EDIT_CUES), window.tick_sync
        )
        assert window.require_cue_edit_rights()
        by_id = {p["machine_id"]: p for p in host.snapshot()}
        assert all(
            p.colour == by_id[p.machine_id]["colour"] for p in window.sync_session.peers()
        )
        capture(window, app, tmp_path / "sync-joined.png")
        host.stop()
        # No UI tick: mutation guards must consult the newest network state.
        wait_until(app, lambda: window.live_client.snapshot()["state"] == "disconnected")
        assert not window.require_cue_edit_rights()
        assert window.sync_session.state == SessionState.DISCONNECTED
        assert "MẤT KẾT NỐI" in labels(window)
        assert "Máy chính" not in labels(window)
        assert not any(b.text() == "Tạo phiên" for b in window.findChildren(QPushButton))
        window.undo_last_cue_action()
        window.add_cue()
        window.add_cue_type()
        window.delete_selected_cue()
        window.delete_cue_type("Light")
        window.rename_cue_type("Light", "Changed")
        window.reorder_cue_types([])
        window.set_cue_type_locked("Light", True)
        window.set_cue_type_visible("Light", False)
        window.set_cue_type_sync("Light", True)
        window.set_cue_type_marker_shape("Light", "circle")
        assert window._undo_stack == [{"description": "Must stay untouched"}]
        assert window.cue_type_defs == [("Light", "#ffffff")]
        assert window.cue_type_meta == {"Light": {"locked": False}}
        assert window.cues_by_track == {"local.wav": []}
        capture(window, app, tmp_path / "sync-disconnected.png")
        print(f"Sync screenshots: {tmp_path}")
    finally:
        release.set()
        window.live_client.stop(notify=False)
        wait_until(app, lambda: not window.live_client.running)
        host.stop()


def test_import_csv_is_locked_like_every_other_cue_edit():
    """CSV import rewrites the whole cue list — the largest cue edit there is
    — and it was the one mutation a locked viewer could still reach. It must
    ask the same gate first, before any dialog or file access."""
    from tracyy.controllers.playlist import PlaylistControllerMixin

    class Shell:
        asked = 0

        def require_cue_edit_rights(self):
            self.asked += 1
            return False

        def main_track_path(self):
            raise AssertionError("the rights gate must run before anything else")

    shell = Shell()
    PlaylistControllerMixin.import_cue_csv(shell)
    assert shell.asked == 1


def test_waveform_signal_preserves_string_cue_id(app):
    from tracyy.widgets.waveform import WaveformWidget

    waveform = WaveformWidget()
    received = []
    waveform.marker_move_finished.connect(
        lambda cue, position: received.append((cue, position))
    )
    waveform.marker_move_finished.emit("machine-a-42", 3.25)
    assert received == [("machine-a-42", 3.25)]
    waveform.deleteLater()


def test_adding_and_renaming_a_cue_accepts_the_new_string_id(window):
    window.id_origin = "test-machine"
    window.cue_table = QTableWidget(0, 4, window)
    window._updating_cue_table = False
    window._cue_id_to_cue = {}
    window._complete_pending_cue_table_now = lambda: None
    window.push_cue_undo = lambda *_args: None
    window.rebuild_cue_runtime_index = lambda *_args, **_kwargs: None
    window._refresh_waveform_markers_direct = lambda: None
    window.save_cue_csv = lambda: None
    window.sync_cue_time_editor_from_selection = lambda: None
    window._update_single_cue_row = lambda _cue_id: None

    def insert_row(row, cue):
        window.cue_table.insertRow(row)
        item = QTableWidgetItem(str(cue["name"]))
        item.setData(Qt.ItemDataRole.UserRole, cue["id"])
        window.cue_table.setItem(row, 2, item)

    window._insert_single_cue_row = insert_row
    window.add_cue()
    cue = window.cues_by_track["local.wav"][0]
    assert cue["id"].startswith("test-machine-")
    assert cue["seconds"] == 12.0
    item = window.cue_table.item(0, 2)
    item.setText("")
    window.on_cue_item_changed(item)
    assert cue["name"] == "Cue 1"
    assert window.errors == []
