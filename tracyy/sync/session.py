"""The live session this machine is in, and who else is in it.

One object owns the answer to three questions the rest of the application
keeps asking: are we in a session, who else is here, and am I allowed to do
this. The network transport is not here — it will hand peers and operations
to this object, and this object will never open a socket itself. Keeping that
line means the page below can be built and tested before any of it exists.
"""

from __future__ import annotations

import time
import uuid

from PySide6.QtCore import QObject, QSettings, Signal

from ..core.ids import track_key
from .model import (
    Capability,
    SessionState,
    SyncPeer,
    SyncRole,
    new_session_code,
    peer_colour_for,
)

__all__ = [
    "SyncSession",
    "local_display_name",
    "local_machine_id",
    "set_local_display_name",
    "settings_store",
]

_SETTINGS_ORG = "Tracyy"
_SETTINGS_APP = "Tracyy"
_MACHINE_KEY = "sync/machine_id"
_NAME_KEY = "sync/display_name"

#: A peer that has not been heard from for this long is treated as gone. Long
#: enough to ride out a laptop lid closing for a moment, short enough that the
#: list does not show people who left the room.
PEER_TIMEOUT_SECONDS = 12.0


def settings_store() -> QSettings:
    """The store this machine's Tracyy Live preferences live in.

    Built through one function so a test can redirect it. ``setDefaultFormat``
    plus ``setPath`` looks like it should be enough and is not: on macOS a
    ``QSettings(org, app)`` stays NativeFormat and writes to
    ``~/Library/Preferences`` regardless, so a test that trusted those two
    calls wrote its fake names straight into the developer's real settings.
    Patching this function is the only isolation that holds on every platform.
    """
    return QSettings(_SETTINGS_ORG, _SETTINGS_APP)


def local_machine_id() -> str:
    """A stable id for this machine, created once and kept.

    Not the licence dongle's serial: that identifies a *licence*, is absent on
    machines running in trial, and raises when no dongle is present. This only
    has to tell two laptops apart in a list of names, so it lives in the same
    settings store as every other per-machine preference.
    """
    settings = settings_store()
    stored = str(settings.value(_MACHINE_KEY, "", type=str) or "").strip()
    if stored:
        return stored

    created = uuid.uuid4().hex[:12]
    settings.setValue(_MACHINE_KEY, created)
    return created


def local_display_name() -> str:
    settings = settings_store()
    stored = str(settings.value(_NAME_KEY, "", type=str) or "").strip()
    return stored or "Máy này"


def set_local_display_name(name: str) -> str:
    cleaned = " ".join(str(name or "").split())[:32]
    if not cleaned:
        cleaned = "Máy này"
    settings_store().setValue(_NAME_KEY, cleaned)
    return cleaned


class SyncSession(QObject):
    """State of one Tracyy Live session, from this machine's point of view."""

    #: Anything about the session or the people in it changed. One signal
    #: rather than several: the page redraws a small list, and a handful of
    #: fine-grained signals would only invite the page to redraw it twice.
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state = SessionState.OFFLINE
        self._session_name = ""
        self._session_code = ""
        self._host_address = ""
        self._peers: dict[str, SyncPeer] = {}
        self._machine_id = local_machine_id()
        self._join_counter = 0

    # -- what this machine is doing -------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_live(self) -> bool:
        return self._state in {SessionState.HOSTING, SessionState.JOINED}

    @property
    def session_name(self) -> str:
        return self._session_name

    @property
    def session_code(self) -> str:
        return self._session_code

    @property
    def host_address(self) -> str:
        return self._host_address

    @property
    def machine_id(self) -> str:
        return self._machine_id

    # -- the people in it ------------------------------------------------

    def peers(self) -> list[SyncPeer]:
        """Everyone in the session, this machine first, then by join order.

        The local machine is pinned to the top because the list is read to
        answer "what am I allowed to do here?" at least as often as "who else
        is on?", and hunting for your own row is a poor way to answer it.
        """
        ordered = sorted(
            self._peers.values(),
            key=lambda peer: (0 if peer.is_local else 1, peer.display_name.lower()),
        )
        return ordered

    def peer(self, machine_id: str) -> SyncPeer | None:
        return self._peers.get(str(machine_id))

    @property
    def local_peer(self) -> SyncPeer | None:
        return self._peers.get(self._machine_id)

    @property
    def local_role(self) -> SyncRole:
        peer = self.local_peer
        return peer.role if peer is not None else SyncRole.VIEWER

    def local_can(self, capability: Capability | str) -> bool:
        """The check the rest of the application should call.

        Offline is deliberately *allowed*: a machine working on its own
        project is not restricted by a session it is not in. Restrictions
        start the moment it asks to join one — the test on OFFLINE rather
        than on ``is_live`` is the point, because a machine still connecting
        is in a session and must not enable controls that are about to be
        refused.
        """
        if self._state == SessionState.OFFLINE:
            return True
        # Dropped means locked, whatever role you were given: the host cannot
        # see what you are doing and cannot stop you, so nothing you change
        # now could be reconciled honestly.
        if self._state == SessionState.DISCONNECTED:
            return False
        peer = self.local_peer
        return peer is not None and peer.can(capability)

    # -- joining and leaving ---------------------------------------------

    def start_hosting(
        self,
        session_name: str,
        host_address: str = "",
        code: str = "",
    ) -> None:
        """Become the host.

        The code may be supplied so the socket and the session agree on it —
        the server has to be listening on a code before the page shows one, or
        the page advertises a code nothing will accept.
        """
        self._state = SessionState.HOSTING
        self._session_name = " ".join(str(session_name or "").split()) or "Phiên không tên"
        self._session_code = str(code or "").strip().upper() or new_session_code()
        self._host_address = str(host_address or "")
        self._peers.clear()
        self._join_counter = 0
        # The host is the owner by construction. Nobody grants it, and nobody
        # can take it away over the network.
        self._add_peer(
            SyncPeer(
                machine_id=self._machine_id,
                display_name=local_display_name(),
                role=SyncRole.OWNER,
                is_local=True,
            )
        )
        self.changed.emit()

    def join(self, session_name: str, code: str, host_address: str) -> None:
        self._state = SessionState.CONNECTING
        self._session_name = " ".join(str(session_name or "").split())
        self._session_code = str(code or "").strip().upper()
        self._host_address = str(host_address or "")
        self._peers.clear()
        self._join_counter = 0
        self._add_peer(
            SyncPeer(
                machine_id=self._machine_id,
                display_name=local_display_name(),
                # Until the host says otherwise, assume the least. A machine
                # that briefly believes it may edit will show enabled controls
                # that then fail, which reads as a broken app.
                role=SyncRole.VIEWER,
                is_local=True,
            )
        )
        self.changed.emit()

    def mark_disconnected(self) -> None:
        """Lost the link to the host. Only a client can be in this state.

        The host does not disconnect from itself: it still owns the document
        and may keep working, it simply has nobody listening.
        """
        if self._state in {SessionState.JOINED, SessionState.CONNECTING}:
            self._state = SessionState.DISCONNECTED
            for peer in self._peers.values():
                if not peer.is_local:
                    # The id goes, the name stays. Losing the link means this
                    # machine no longer knows what anybody has open, so every
                    # cursor and playhead drawn from it has to disappear
                    # rather than freeze somewhere misleading; the name is
                    # display-only and the disconnected state already says it
                    # is no longer live.
                    peer.track_id = ""
                    peer.editing_cue_id = ""
                    peer.editing_column = ""
            self.changed.emit()

    def mark_joined(self) -> None:
        if self._state in {SessionState.CONNECTING, SessionState.DISCONNECTED}:
            self._state = SessionState.JOINED
            self.changed.emit()

    def leave(self) -> None:
        if self._state == SessionState.OFFLINE and not self._peers:
            return
        self._state = SessionState.OFFLINE
        self._session_name = ""
        self._session_code = ""
        self._host_address = ""
        self._peers.clear()
        self._join_counter = 0
        self.changed.emit()

    # -- peers arriving, changing and leaving -----------------------------

    def _add_peer(self, peer: SyncPeer) -> SyncPeer:
        peer.colour = peer_colour_for(self._join_counter)
        peer.last_seen = time.monotonic()
        self._join_counter += 1
        self._peers[peer.machine_id] = peer
        return peer

    def upsert_peer(self, peer: SyncPeer) -> None:
        """Add a peer, or refresh one already here.

        Colour and join order survive the refresh: a peer whose colour changed
        because they sent an update would make every mark they had left on the
        waveform appear to belong to somebody else.
        """
        existing = self._peers.get(peer.machine_id)
        if existing is None:
            self._add_peer(peer)
        else:
            peer.colour = existing.colour
            peer.is_local = existing.is_local
            peer.last_seen = time.monotonic()
            self._peers[peer.machine_id] = peer
        self.changed.emit()

    def apply_network_peers(self, payloads: list[dict], session_name: str = "") -> None:
        """Apply one authoritative roster, preserving the host's colours.

        Positions can change each tick; only visible row changes redraw the
        list. One signal also prevents partially updated role/peer displays.
        """
        def visible(peers):
            return sorted((p.machine_id, p.display_name, p.role.value, p.colour,
                           p.track_name) for p in peers.values())

        before = visible(self._peers)
        updated = {}
        for raw in payloads:
            peer = SyncPeer.from_payload(raw)
            if peer.machine_id:
                peer.is_local = peer.machine_id == self._machine_id
                peer.last_seen = time.monotonic()
                updated[peer.machine_id] = peer
        if self._machine_id not in updated and self.local_peer is not None:
            updated[self._machine_id] = self.local_peer
        self._peers = updated
        name_changed = bool(session_name) and session_name != self._session_name
        if name_changed:
            self._session_name = session_name
        if name_changed or before != visible(updated):
            self.changed.emit()

    def remove_peer(self, machine_id: str) -> None:
        if str(machine_id) == self._machine_id:
            return
        if self._peers.pop(str(machine_id), None) is not None:
            self.changed.emit()

    def touch_peer(self, machine_id: str) -> None:
        """Note that a peer is still there, without redrawing anything."""
        peer = self._peers.get(str(machine_id))
        if peer is not None:
            peer.last_seen = time.monotonic()

    def drop_silent_peers(self, timeout: float = PEER_TIMEOUT_SECONDS) -> int:
        """Forget peers that stopped talking. Returns how many went."""
        if not self.is_live:
            return 0

        cutoff = time.monotonic() - max(1.0, float(timeout))
        gone = [
            machine_id
            for machine_id, peer in self._peers.items()
            if not peer.is_local and peer.last_seen < cutoff
        ]
        for machine_id in gone:
            self._peers.pop(machine_id, None)
        if gone:
            self.changed.emit()
        return len(gone)

    # -- roles -------------------------------------------------------------

    def set_peer_role(self, machine_id: str, role: SyncRole | str) -> bool:
        """Change what a peer may do. Only the owner may call this.

        Returns whether anything changed, so a caller can tell a refused
        change from a no-op instead of having to compare the list itself.
        """
        if not self.local_can(Capability.MANAGE_PEOPLE):
            return False

        peer = self._peers.get(str(machine_id))
        if peer is None:
            return False

        try:
            resolved = SyncRole(role)
        except ValueError:
            return False

        # The owner cannot demote itself out of the session it is hosting;
        # doing so would leave nobody able to grant the role back.
        if peer.is_local and self._state == SessionState.HOSTING:
            return False

        if peer.role == resolved:
            return False

        peer.role = resolved
        self.changed.emit()
        return True

    def rename_local(self, name: str) -> str:
        cleaned = set_local_display_name(name)
        peer = self.local_peer
        if peer is not None and peer.display_name != cleaned:
            peer.display_name = cleaned
            self.changed.emit()
        return cleaned

    # -- presence ----------------------------------------------------------

    def update_presence(
        self,
        machine_id: str,
        track_id: str | None = None,
        track_name: str | None = None,
        position_seconds: float | None = None,
        editing_cue_id: str | None = None,
        editing_column: str | None = None,
    ) -> None:
        """Record where a peer is. Never emits ``changed``.

        Presence arrives about twenty times a second per machine. Emitting a
        change signal for each would repaint the people list a hundred times a
        second to move a number nobody is reading; the waveform overlay reads
        these fields directly on its own repaint instead.
        """
        peer = self._peers.get(str(machine_id))
        if peer is None:
            return
        if track_id is not None:
            peer.track_id = str(track_id)
        if track_name is not None:
            peer.track_name = str(track_name)
        if editing_cue_id is not None:
            peer.editing_cue_id = str(editing_cue_id)
        if editing_column is not None:
            peer.editing_column = str(editing_column)
        if position_seconds is not None:
            try:
                peer.position_seconds = float(position_seconds)
            except (TypeError, ValueError):
                pass
        peer.last_seen = time.monotonic()

    def peers_editing(self, track_id: str) -> list[SyncPeer]:
        """Everyone except you with a cell open in an editor on ``track_id``.

        A subset of :meth:`peers_on_track`, not a separate source of truth:
        somebody who wandered off to another song stops having a cell here the
        moment their track id changes, without anything having to remember to
        clear it.
        """
        return [
            peer
            for peer in self.peers_on_track(track_id)
            if peer.editing_cue_id and peer.editing_column
        ]

    def peers_on_track(self, track_id: str) -> list[SyncPeer]:
        """Everyone except you who has ``track_id`` open.

        By id rather than by path: the presence overlay asks this about the
        song on screen, and the machine that answered may be a Windows laptop
        holding that song at a path this one has never seen.
        """
        wanted = track_key(track_id)
        if not wanted:
            return []
        return [
            peer
            for peer in self.peers()
            if not peer.is_local and track_key(peer.track_id) == wanted
        ]
