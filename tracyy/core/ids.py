"""Identifiers for cues and tracks that survive leaving this machine.

Until version 4 a cue's id was ``max(existing ids on this track) + 1`` and a
track had no id at all — it was addressed by its path on disk. Both work
perfectly for one person on one laptop and fail silently the moment there are
two:

*Two people adding a cue to the same track both mint the same number.* From
that point the two machines mean different cues by one name, and nothing
errors.

*The same song has different paths on different machines* — and different
separators on macOS and Windows — so an edit addressed by path lands nowhere
on the other side. The project loader already knows this and remaps cue keys
when a project is opened somewhere else; live editing needs the same knowledge
one level deeper, in the identifier itself.

So both become opaque strings minted here. Nothing reads their shape: they are
compared, used as dictionary keys, and written to the project file. The one
concession to a human is the origin prefix, which makes it possible to see at
a glance that a run of cues came from the same machine.

No Qt: worker processes and the sync server import this.
"""

from __future__ import annotations

import itertools
import secrets

__all__ = [
    "cue_key",
    "new_cue_id",
    "new_origin",
    "new_track_id",
    "track_key",
]

#: Counter shared by every id minted in this process. Paired with a random
#: origin rather than persisted: a counter written to disk would have to be
#: read, locked and flushed on every cue, and restarting after a crash would
#: risk handing out a number twice. A fresh origin per run makes that
#: impossible without storing anything.
_counter = itertools.count(1)


def new_origin(length: int = 6) -> str:
    """A short random token identifying one run, or one migrated document."""
    return secrets.token_hex(max(2, int(length) // 2))


def new_cue_id(origin: str) -> str:
    """A cue id unique across every machine in a session."""
    return f"{origin}-{next(_counter)}"


def new_track_id(origin: str) -> str:
    """A track id that means the same song on every machine."""
    return f"t{origin}-{next(_counter)}"


def cue_key(value: object) -> str:
    """Normalise a cue id for comparison and for use as a dictionary key.

    Everything that identifies a cue goes through here, so a v3 integer read
    from an old project, a string from the wire, and a value pulled out of a
    table's item data all compare equal when they name the same cue. Without
    it the mixed types would silently fail to match and a cue would look like
    it had vanished.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        # Guard before the int branch: bool is an int subclass, and "True"
        # is not an id anybody meant to write.
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # A float id can only have come from JSON round-tripping an integer.
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def track_key(value: object) -> str:
    """Normalise a track id. Same contract as :func:`cue_key`."""
    return cue_key(value)
