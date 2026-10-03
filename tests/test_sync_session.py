"""Tracyy Live: what each role may do, and how the peer list behaves.

Roles decide whether a machine across the room can change the show, so the
rules get tests rather than a careful reading. The session object is pure
state — no sockets — which is exactly why it can be tested at all.
"""

from __future__ import annotations

import tempfile
import uuid

import pytest
from PySide6.QtCore import QCoreApplication, QSettings

import tracyy.controllers.sync as sync_controller
import tracyy.sync.session as session_module
from tracyy.sync.model import (
    ROLE_CAPABILITIES,
    Capability,
    SessionState,
    SyncPeer,
    SyncRole,
    new_session_code,
    role_allows,
)
from tracyy.sync.session import SyncSession

#: Every settings write in these tests lands here instead of the real store.
_SETTINGS_DIR = tempfile.mkdtemp(prefix="tracyy-sync-tests-")

# setPath *does* redirect a QSettings constructed with an explicit IniFormat,
# which is what the fixture below builds. It is only the (org, app) form that
# ignores it on macOS.
QSettings.setPath(
    QSettings.Format.IniFormat,
    QSettings.Scope.UserScope,
    _SETTINGS_DIR,
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Keep the tests out of the developer's real Tracyy preferences.

    Redirected by replacing the store factory, not by ``setDefaultFormat`` and
    ``setPath``: on macOS those leave ``QSettings(org, app)`` on NativeFormat
    writing to ``~/Library/Preferences``, and the first version of this fixture
    renamed the developer's own machine because of it.
    """
    unique = uuid.uuid4().hex[:8]

    def store():
        return QSettings(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            f"TracyyTest-{unique}",
            f"TracyyTest-{unique}",
        )

    monkeypatch.setattr(session_module, "settings_store", store)
    monkeypatch.setattr(sync_controller, "settings_store", store)
    yield


def test_the_tests_do_not_touch_the_real_preferences() -> None:
    """The isolation above is load-bearing, so it gets its own test."""
    assert str(session_module.settings_store().fileName()).startswith(_SETTINGS_DIR)


@pytest.fixture
def app():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def session(app):
    return SyncSession()


# -- roles -------------------------------------------------------------


def test_owner_may_do_everything() -> None:
    assert ROLE_CAPABILITIES[SyncRole.OWNER] == frozenset(Capability)


def test_viewer_may_do_nothing() -> None:
    assert ROLE_CAPABILITIES[SyncRole.VIEWER] == frozenset()


def test_cue_only_cannot_touch_the_running_order() -> None:
    """The lighting operator gets cues without getting the show's shape."""
    assert role_allows(SyncRole.CUE_ONLY, Capability.EDIT_CUES)
    assert not role_allows(SyncRole.CUE_ONLY, Capability.EDIT_PLAYLIST)
    assert not role_allows(SyncRole.CUE_ONLY, Capability.REPLACE_MEDIA)


def test_an_unknown_role_is_allowed_nothing() -> None:
    """A packet naming a role this build has never heard of must not open a door."""
    assert not role_allows("superuser", Capability.EDIT_CUES)
    assert not role_allows(SyncRole.OWNER, "delete_everything")


def test_unknown_role_on_the_wire_degrades_to_viewer() -> None:
    peer = SyncPeer.from_payload({"machine_id": "x", "display_name": "X", "role": "boss"})
    assert peer.role is SyncRole.VIEWER


def test_session_codes_avoid_characters_that_get_misread() -> None:
    """Codes are read aloud across a room; O/0 and I/1/L are the classic mix-ups."""
    codes = "".join(new_session_code() for _ in range(200))
    assert not set(codes) & set("O0I1LS5")


# -- what this machine may do ------------------------------------------


def test_working_alone_is_never_restricted(session: SyncSession) -> None:
    assert session.state is SessionState.OFFLINE
    for capability in Capability:
        assert session.local_can(capability)


def test_hosting_makes_you_the_owner(session: SyncSession) -> None:
    session.start_hosting("Show")
    assert session.local_role is SyncRole.OWNER
    assert session.local_can(Capability.MANAGE_PEOPLE)


def test_joining_starts_with_no_rights_until_the_host_says_otherwise(
    session: SyncSession,
) -> None:
    """Assume the least: controls that enable and then fail read as a broken app."""
    session.join("Show", "ABC123", "192.168.1.9:8090")
    assert session.local_role is SyncRole.VIEWER
    assert not session.local_can(Capability.EDIT_CUES)


def test_only_the_owner_may_change_roles(session: SyncSession) -> None:
    session.join("Show", "ABC123", "192.168.1.9:8090")
    session.mark_joined()
    session.upsert_peer(SyncPeer(machine_id="other", display_name="Hùng"))

    assert not session.set_peer_role("other", SyncRole.EDITOR)
    assert session.peer("other").role is SyncRole.VIEWER


def test_the_host_cannot_demote_itself(session: SyncSession) -> None:
    """Otherwise nobody is left who can hand the role back."""
    session.start_hosting("Show")
    assert not session.set_peer_role(session.machine_id, SyncRole.VIEWER)
    assert session.local_role is SyncRole.OWNER


def test_owner_can_promote_someone(session: SyncSession) -> None:
    session.start_hosting("Show")
    session.upsert_peer(SyncPeer(machine_id="other", display_name="Hùng"))

    assert session.set_peer_role("other", SyncRole.EDITOR)
    assert session.peer("other").can(Capability.EDIT_CUES)
    assert not session.peer("other").can(Capability.MANAGE_PEOPLE)


# -- the peer list -----------------------------------------------------


def test_a_peer_keeps_its_colour_across_an_update(session: SyncSession) -> None:
    """A colour that moved would reassign every mark that peer left behind."""
    session.start_hosting("Show")
    session.upsert_peer(SyncPeer(machine_id="other", display_name="Hùng"))
    original = session.peer("other").colour

    session.upsert_peer(SyncPeer(machine_id="other", display_name="Hùng Đã Đổi Tên"))
    assert session.peer("other").colour == original
    assert session.peer("other").display_name == "Hùng Đã Đổi Tên"


def test_peers_get_different_colours(session: SyncSession) -> None:
    session.start_hosting("Show")
    for index in range(4):
        session.upsert_peer(SyncPeer(machine_id=f"m{index}", display_name=f"P{index}"))

    colours = [peer.colour for peer in session.peers()]
    assert len(set(colours)) == len(colours)


def test_you_are_first_in_the_list(session: SyncSession) -> None:
    session.start_hosting("Show")
    session.rename_local("Zzz")
    session.upsert_peer(SyncPeer(machine_id="other", display_name="Aaa"))

    assert session.peers()[0].is_local


def test_silent_peers_are_dropped_but_you_are_not(session: SyncSession) -> None:
    session.start_hosting("Show")
    session.upsert_peer(SyncPeer(machine_id="other", display_name="Hùng"))
    session.peer("other").last_seen = 0.0

    assert session.drop_silent_peers(timeout=1.0) == 1
    assert session.peer("other") is None
    assert session.local_peer is not None


def test_presence_updates_do_not_redraw_the_page(session: SyncSession) -> None:
    """Presence arrives ~20 times a second per machine.

    Emitting ``changed`` for each would rebuild the people list a hundred
    times a second to move a number nobody reads.
    """
    session.start_hosting("Show")
    session.upsert_peer(SyncPeer(machine_id="other", display_name="Hùng"))

    redraws = []
    session.changed.connect(lambda: redraws.append(1))

    for step in range(30):
        session.update_presence("other", track_id="t-a", position_seconds=step)

    assert redraws == []
    assert session.peer("other").position_seconds == 29.0


def test_peers_on_track_excludes_you(session: SyncSession) -> None:
    session.start_hosting("Show")
    session.upsert_peer(SyncPeer(machine_id="other", display_name="Hùng"))
    session.update_presence("other", track_id="t-a")
    session.update_presence(session.machine_id, track_id="t-a")

    on_track = session.peers_on_track("t-a")
    assert [peer.machine_id for peer in on_track] == ["other"]


def test_leaving_clears_everything(session: SyncSession) -> None:
    session.start_hosting("Show")
    session.upsert_peer(SyncPeer(machine_id="other", display_name="Hùng"))
    session.leave()

    assert session.state is SessionState.OFFLINE
    assert session.peers() == []
    assert session.session_code == ""
