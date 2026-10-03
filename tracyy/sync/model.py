"""Roles, capabilities and peers for a Tracyy Live session.

Deliberately free of Qt. The sync server will run as its own process, the way
the LAN cue viewer already does, and it has to answer "is this machine allowed
to do that?" without dragging the GUI stack in with it. Everything here is
plain data plus the rules that read it; :mod:`tracyy.sync.session` is the Qt
side that owns one of these and tells the window when it changed.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "PEER_COLOURS",
    "ROLE_CAPABILITIES",
    "ROLE_LABELS",
    "Capability",
    "SessionState",
    "SyncPeer",
    "SyncRole",
    "colour_on_dark",
    "new_session_code",
    "peer_colour_for",
    "role_allows",
]


class SessionState(str, Enum):
    """Where this machine stands in a session."""

    OFFLINE = "offline"
    HOSTING = "hosting"
    JOINED = "joined"
    CONNECTING = "connecting"
    #: Was in a session and lost the link. Deliberately not the same as
    #: OFFLINE: a machine that left is working on its own project and may do
    #: anything, while a machine that dropped is still looking at somebody
    #: else's show and must not change it behind their back.
    DISCONNECTED = "disconnected"


class SyncRole(str, Enum):
    OWNER = "owner"
    EDITOR = "editor"
    CUE_ONLY = "cue_only"
    VIEWER = "viewer"


class Capability(str, Enum):
    """One thing a machine may or may not be allowed to do.

    Capabilities rather than role comparisons at the call site: a check reads
    ``session.local_can(Capability.EDIT_CUES)``, which stays true when the
    roles are renamed or a fifth one is added, and cannot be got the wrong way
    round the way ``role != VIEWER`` can.
    """

    EDIT_CUES = "edit_cues"
    EDIT_PLAYLIST = "edit_playlist"
    REPLACE_MEDIA = "replace_media"
    MANAGE_PEOPLE = "manage_people"


#: What each role may do. Roles are presets over capabilities, not the other
#: way round — the show's lighting operator gets cues without also getting the
#: power to reorder the running order.
ROLE_CAPABILITIES: dict[SyncRole, frozenset[Capability]] = {
    SyncRole.OWNER: frozenset(Capability),
    SyncRole.EDITOR: frozenset(
        {
            Capability.EDIT_CUES,
            Capability.EDIT_PLAYLIST,
        }
    ),
    SyncRole.CUE_ONLY: frozenset({Capability.EDIT_CUES}),
    SyncRole.VIEWER: frozenset(),
}

ROLE_LABELS: dict[SyncRole, str] = {
    SyncRole.OWNER: "Chủ phiên",
    SyncRole.EDITOR: "Biên tập",
    SyncRole.CUE_ONLY: "Chỉ sửa cue",
    SyncRole.VIEWER: "Chỉ xem",
}

#: Identity colours, assigned by join order. Chosen to stay apart from the
#: application's own green and from each other, including for the two most
#: common kinds of colour blindness — several people's marks sit on one
#: waveform at once, so telling them apart is the whole point.
PEER_COLOURS: tuple[str, ...] = (
    "#4C8DF6",
    "#E0609C",
    "#F0A030",
    "#33B9A6",
    "#A874E8",
    "#E8615A",
    "#5FB84B",
    "#D8C24A",
)

#: Session codes are read aloud and typed across a room, so the alphabet drops
#: every pair that gets misheard or misread: no O/0, no I/1/L, no S/5.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRTUVWXYZ2346789"


def role_allows(role: SyncRole | str, capability: Capability | str) -> bool:
    """Whether ``role`` may do ``capability``. Unknown role means no."""
    try:
        resolved_role = SyncRole(role)
        resolved_capability = Capability(capability)
    except ValueError:
        return False
    return resolved_capability in ROLE_CAPABILITIES.get(resolved_role, frozenset())


def colour_on_dark(colour: str, amount: float = 0.34) -> str:
    """A lighter form of an identity colour, for thin marks on a black wave.

    The people list draws a solid 8px dot, where a mid-tone reads fine. The
    waveform draws a 1.1px dashed line over near-black, where that same colour
    turns to mud. Mixing toward white keeps the hue — the thing people
    actually tell each other apart by — while lifting it far enough to survive
    a single pixel. Unparseable input comes back unchanged rather than
    raising: a colour is decoration, and none is worth a traceback mid-show.
    """
    text = str(colour or "").strip().lstrip("#")
    if len(text) != 6:
        return str(colour or "")
    try:
        channels = [int(text[index : index + 2], 16) for index in (0, 2, 4)]
    except ValueError:
        return str(colour or "")
    weight = max(0.0, min(1.0, float(amount)))
    lifted = [round(value + (255 - value) * weight) for value in channels]
    return "#" + "".join(f"{value:02X}" for value in lifted)


def peer_colour_for(index: int) -> str:
    """A stable colour for the ``index``-th peer to join."""
    if not PEER_COLOURS:
        return "#4C8DF6"
    return PEER_COLOURS[int(index) % len(PEER_COLOURS)]


def new_session_code(length: int = 6) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(max(4, int(length))))


@dataclass
class SyncPeer:
    """One machine in the session, as everyone else sees it."""

    machine_id: str
    display_name: str
    role: SyncRole = SyncRole.VIEWER
    colour: str = PEER_COLOURS[0]
    #: Track this machine currently has open, as the id the session agrees
    #: on. Empty means "no track", which is a normal state, not an error.
    #: Never a file path: the same song sits at different paths on macOS and
    #: Windows, so a path from another machine matches nothing here — and
    #: sending one hands the whole room a tour of somebody else's disk.
    track_id: str = ""
    #: What to call that track on a machine that cannot resolve the id — a
    #: peer may have opened a song this one has never seen. Display only;
    #: nothing is ever looked up by it.
    track_name: str = ""
    #: Playhead in seconds, mirrored for the presence layer.
    position_seconds: float = 0.0
    #: The cell this machine currently has open in an editor, as the cue's id
    #: and the column's key. Empty means nobody is typing. Anchored to the cue
    #: rather than to a row number on purpose: two cues can share a timecode —
    #: real shows have several — and every machine sorts and scrolls its own
    #: table, so a row number means nothing anywhere else.
    editing_cue_id: str = ""
    editing_column: str = ""
    is_local: bool = False
    #: Monotonic clock of the last packet from this machine. The session uses
    #: it to drop peers that stopped talking; it is never shown as a time.
    last_seen: float = 0.0
    extra: dict[str, object] = field(default_factory=dict)

    def can(self, capability: Capability | str) -> bool:
        return role_allows(self.role, capability)

    def as_payload(self) -> dict[str, object]:
        """The wire form. Deliberately omits ``last_seen``, which is local."""
        return {
            "machine_id": self.machine_id,
            "display_name": self.display_name,
            "role": self.role.value,
            "colour": self.colour,
            "track_id": self.track_id,
            "track_name": self.track_name,
            "editing_cue_id": self.editing_cue_id,
            "editing_column": self.editing_column,
            "position_seconds": float(self.position_seconds),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> SyncPeer:
        """Rebuild a peer from the wire.

        Everything here arrives from another machine, so nothing is trusted:
        an unknown role degrades to viewer rather than raising, because a
        malformed packet must not be able to stop the session.
        """
        try:
            role = SyncRole(str(payload.get("role", "")))
        except ValueError:
            role = SyncRole.VIEWER

        return cls(
            machine_id=str(payload.get("machine_id", "") or ""),
            display_name=str(payload.get("display_name", "") or "Không tên"),
            role=role,
            colour=str(payload.get("colour", "") or PEER_COLOURS[0]),
            track_id=str(payload.get("track_id", "") or ""),
            track_name=str(payload.get("track_name", "") or "")[:120],
            editing_cue_id=str(payload.get("editing_cue_id", "") or "")[:120],
            editing_column=str(payload.get("editing_column", "") or "")[:32],
            position_seconds=_as_float(payload.get("position_seconds")),
        )


def _as_float(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return number if number == number else 0.0
