"""Cue content moving between machines: the wire half and the applying half.

Two levels, because the failures live at two levels. Over a real socket:
whether a machine that never says "I applied that" keeps being told again.
In plain Python: whether the same record arriving twice makes two cues.
"""

from __future__ import annotations

import socket
import time

import pytest

from tracyy.controllers.cue import CueControllerMixin
from tracyy.sync.client import LiveClient
from tracyy.sync.server import LiveServer


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait(check, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return None


@pytest.fixture
def host(tmp_path):
    server = LiveServer()
    port = _free_port()
    server.start(
        "127.0.0.1",
        port,
        "Show Thử",
        "ABCD23",
        "host-machine",
        "Chủ Phiên",
        journal_path=tmp_path / "journal.ndjson",
    )
    try:
        yield server, f"127.0.0.1:{port}"
    finally:
        server.stop()


@pytest.fixture
def editor(host):
    """A joined client with permission to edit cues."""
    server, address = host
    client = LiveClient()
    client.start(address, "ABCD23", "laptop-1", "Linh")
    try:
        assert _wait(lambda: client.snapshot()["state"] == "joined")
        server.set_role("laptop-1", "editor")
        assert _wait(lambda: client.snapshot()["you"].get("role") == "editor")
        yield client
    finally:
        client.stop(notify=False)


def _add(cue_id: str, seconds: float, name: str = "") -> dict:
    return {
        "op_id": f"op-{cue_id}",
        "track_id": "t-1",
        "cue_id": cue_id,
        "kind": "add_cue",
        "payload": {"seconds": seconds, "name": name or f"Cue {cue_id}"},
    }


# -- over the socket ---------------------------------------------------------


def test_the_host_writes_it_down_and_hands_it_back(host, editor) -> None:
    server, _address = host
    assert editor.submit_operation(_add("c-1", 12.0))["ok"]

    records = _wait(lambda: editor.take_ops())
    assert records is not None, "the commit never came back on a heartbeat"
    assert [record["cue_id"] for record in records] == ["c-1"]
    assert records[0]["author"] == "laptop-1"
    assert server.revision == 1


def test_unapplied_records_keep_coming_back(host, editor) -> None:
    """Taking them is not acknowledging them.

    A machine that took a batch and then died must be told again when it
    returns, or the cue it never wrote down is gone from that machine for the
    rest of the session with nothing to show for it.
    """
    assert editor.submit_operation(_add("c-1", 3.0))["ok"]
    assert _wait(lambda: editor.take_ops()) is not None

    # Never confirmed, so the host is still owed an acknowledgement.
    again = _wait(lambda: editor.take_ops())
    assert again is not None and again[0]["cue_id"] == "c-1"

    editor.confirm_applied(again[0]["revision"])
    time.sleep(0.4)
    assert editor.take_ops() == []


def test_the_host_refuses_an_edit_to_a_cue_somebody_changed_first(host, editor) -> None:
    server, _address = host
    assert editor.submit_operation(_add("c-1", 5.0))["ok"]

    # The host itself moves that cue, so the client's view is now behind.
    assert server.commit_local(
        {
            "op_id": "op-host-move",
            "track_id": "t-1",
            "cue_id": "c-1",
            "kind": "move_cue",
            "payload": {"seconds": 9.0},
        }
    )["ok"]

    stale = editor.submit_operation(
        {
            "op_id": "op-stale",
            "track_id": "t-1",
            "cue_id": "c-1",
            "kind": "move_cue",
            "base_revision": 1,
            "payload": {"seconds": 4.0},
        }
    )
    assert not stale["ok"]
    assert stale["reason"] == "stale_revision"


def test_a_viewer_is_refused_by_the_host_not_by_a_grey_button(host) -> None:
    _server, address = host
    client = LiveClient()
    client.start(address, "ABCD23", "laptop-2", "Khách")
    try:
        assert _wait(lambda: client.snapshot()["state"] == "joined")
        reply = client.submit_operation(_add("c-9", 1.0))
        assert not reply["ok"]
        assert reply["reason"] == "forbidden"
    finally:
        client.stop(notify=False)


def test_the_host_commits_through_the_same_document(host) -> None:
    server, _address = host
    assert server.commit_local(_add("c-h", 2.0))["ok"]
    assert server.revision == 1
    # A second commit of the same op_id is the same commit, not a second cue.
    repeat = server.commit_local(_add("c-h", 2.0))
    assert repeat["ok"] and server.revision == 1


def test_the_host_is_told_what_the_clients_committed(host, editor) -> None:
    """The one screen that can miss an edit is the host's own.

    It owns the document, so the change is saved, journalled and already on
    its way to everybody else — and the window belonging to the person running
    the show is the one nothing ever updated.
    """
    server, _address = host
    assert editor.submit_operation(_add("c-1", 4.0, "Cue 01"))["ok"]
    assert editor.submit_operation(
        {
            "op_id": "op-label",
            "track_id": "t-1",
            "cue_id": "c-1",
            "kind": "update_cue_fields",
            "payload": {"label": "Khói vào"},
        }
    )["ok"]

    records = _wait(lambda: server.take_host_ops())
    assert records is not None
    kinds = [(record["kind"], record.get("payload", {})) for record in records]
    assert ("update_cue_fields", {"label": "Khói vào"}) in kinds


def test_the_host_is_not_handed_back_its_own_edits(host) -> None:
    """It applied them as it made them; doing it twice is work for nothing."""
    server, _address = host
    assert server.commit_local(_add("c-h", 2.0))["ok"]
    assert server.take_host_ops() == []


def test_cues_that_existed_before_the_session_can_be_edited(host, editor) -> None:
    """The show is almost never empty when somebody presses Create session.

    Without seeding, the document starts blank while the host's window is full
    of cues, and every attempt to touch one of them is refused — correctly and
    incomprehensibly — as a cue that does not exist.
    """
    server, _address = host
    server.seed_document(
        {"t-1": [{"id": "c-old", "seconds": 8.0, "name": "Cue 01", "label": ""}]}
    )

    reply = editor.submit_operation(
        {
            "op_id": "op-name",
            "track_id": "t-1",
            "cue_id": "c-old",
            "kind": "update_cue_fields",
            "payload": {"label": "Mở màn"},
        }
    )
    assert reply["ok"], reply.get("error")


def test_a_machine_joining_late_is_handed_the_whole_sheet(host, editor) -> None:
    """Replaying the journal would give it only what changed since it arrived."""
    server, _address = host
    server.seed_document(
        {"t-1": [{"id": "c-old", "seconds": 8.0, "name": "Cue 01", "label": "Mở màn"}]}
    )
    assert editor.submit_operation(_add("c-new", 20.0, "Cue 02"))["ok"]

    snapshot = editor.fetch_document()
    assert snapshot.get("ok")
    cues = {cue["id"]: cue for cue in snapshot["tracks"]["t-1"]}
    assert set(cues) == {"c-old", "c-new"}
    assert cues["c-old"]["label"] == "Mở màn"
    assert snapshot["revision"] == 1


def test_seeding_refuses_to_rewrite_history(host) -> None:
    """Once anything is committed, the ground under it stops being movable."""
    server, _address = host
    assert server.commit_local(_add("c-1", 3.0))["ok"]
    server.seed_document({"t-1": [{"id": "c-other", "seconds": 9.0, "name": "X"}]})

    snapshot = server._document.snapshot()
    assert [cue["id"] for cue in snapshot["tracks"]["t-1"]] == ["c-1"]


# -- applying, without Qt ----------------------------------------------------


class FakeWindow:
    """Just enough of the window for the apply path to run."""

    MAX_DEFERRED_CUE_OPS = CueControllerMixin.MAX_DEFERRED_CUE_OPS
    deferred_cue_ops = CueControllerMixin.deferred_cue_ops
    apply_remote_cue_ops = CueControllerMixin.apply_remote_cue_ops

    def __init__(self, paths: dict[str, str]) -> None:
        self._paths = paths
        self.cues_by_track: dict[str, list[dict]] = {}
        self.saved: list[str] = []
        self.refreshed: list[bool] = []

    def path_for_track_id(self, track_id: str) -> str:
        return self._paths.get(track_id, "")

    def main_track_path(self) -> str:
        return next(iter(self._paths.values()), "")

    def cue_type_color(self, cue_type: str) -> str:
        return "#ff7a45"

    def save_cue_csv(self, track_path=None) -> None:
        self.saved.append(str(track_path))

    def refresh_after_remote_cue_ops(self, open_track_changed: bool) -> None:
        self.refreshed.append(open_track_changed)


def record(kind: str, cue_id: str, revision: int, **payload) -> dict:
    return {
        "revision": revision,
        "track_id": "t-1",
        "cue_id": cue_id,
        "kind": kind,
        "payload": payload,
    }


@pytest.fixture
def window():
    return FakeWindow({"t-1": "C:/Music/bai.mp3"})


def test_operations_build_the_track_up(window: FakeWindow) -> None:
    window.apply_remote_cue_ops(
        [
            record("add_cue", "c-2", 1, seconds=30.0, name="Cue 2", type="Pyro"),
            record("add_cue", "c-1", 2, seconds=10.0, name="Cue 1", type="Audio"),
            record("update_cue_fields", "c-1", 3, label="Khói vào"),
            record("move_cue", "c-2", 4, seconds=5.0),
        ]
    )
    cues = window.cues_by_track["C:/Music/bai.mp3"]
    # Kept in time order, so the table and the waveform agree without sorting.
    assert [cue["id"] for cue in cues] == ["c-2", "c-1"]
    assert cues[1]["label"] == "Khói vào"
    assert cues[0]["seconds"] == 5.0
    assert window.refreshed == [True]


def test_the_same_record_twice_makes_one_cue(window: FakeWindow) -> None:
    """The host resends anything unacknowledged on every beat."""
    twice = [record("add_cue", "c-1", 1, seconds=10.0, name="Cue 1")] * 2
    window.apply_remote_cue_ops(twice)
    assert len(window.cues_by_track["C:/Music/bai.mp3"]) == 1


def test_deleting_a_cue_this_machine_never_had_is_not_an_error(window: FakeWindow) -> None:
    window.apply_remote_cue_ops([record("delete_cue", "ghost", 1)])
    assert window.cues_by_track["C:/Music/bai.mp3"] == []
    # Nothing to redraw either: an operation that changed nothing must not
    # rebuild a table somebody may be reading.
    assert window.refreshed == []


def test_cues_for_a_song_this_machine_lacks_are_kept(window: FakeWindow) -> None:
    """Media sync is what makes the track appear; the cues have to survive it."""
    window.apply_remote_cue_ops(
        [
            {
                "revision": 1,
                "track_id": "t-elsewhere",
                "cue_id": "c-5",
                "kind": "add_cue",
                "payload": {"seconds": 4.0, "name": "Cue 5"},
            }
        ]
    )
    assert window.cues_by_track == {}
    assert [op["cue_id"] for op in window.deferred_cue_ops()] == ["c-5"]


def test_a_delete_removes_it(window: FakeWindow) -> None:
    window.apply_remote_cue_ops(
        [
            record("add_cue", "c-1", 1, seconds=10.0, name="Cue 1"),
            record("delete_cue", "c-1", 2),
        ]
    )
    assert window.cues_by_track["C:/Music/bai.mp3"] == []
