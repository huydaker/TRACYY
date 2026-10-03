"""Tracyy Live — several machines building one cue list together.

``model`` is plain data and rules, importable anywhere including a worker
process. ``session`` is the Qt object the window talks to; import it from
here only when Qt is already loaded.
"""

from __future__ import annotations

from .model import (
    PEER_COLOURS,
    ROLE_CAPABILITIES,
    ROLE_LABELS,
    Capability,
    SessionState,
    SyncPeer,
    SyncRole,
    new_session_code,
    peer_colour_for,
    role_allows,
)

__all__ = [
    "PEER_COLOURS",
    "ROLE_CAPABILITIES",
    "ROLE_LABELS",
    "Capability",
    "SessionState",
    "SyncPeer",
    "SyncRole",
    "new_session_code",
    "peer_colour_for",
    "role_allows",
]
