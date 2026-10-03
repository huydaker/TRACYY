"""Where a machine keeps what it received from a Tracyy Live session.

Everything a client pulls down — the audio it downloaded, the waveform peaks
the host sent instead of audio, and the last cue snapshot — lands in one
directory per show so it can be shown, measured and deleted as one thing. A
crew laptop that joined three shows should be able to see which one is using
four gigabytes and throw that one away without touching the others.

Deliberately free of Qt: the sync server process will write into the same
layout, and nothing here needs a GUI.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from tracyy.core.paths import cache_root

__all__ = ["ShowStore", "human_bytes", "live_root", "store_for"]

#: Subdirectories inside one show's store. Split by kind rather than mixed,
#: so "delete the audio but keep the cues" stays possible later without
#: having to work out what each file was.
_MEDIA_DIR = "media"
_PEAKS_DIR = "peaks"
_DOCUMENT = "document.json"
_JOURNAL = "journal.ndjson"


def live_root() -> Path:
    """Parent of every show store on this machine.

    Under the cache root on purpose — its docstring says "safe to delete",
    which is exactly the contract: losing this costs a re-download, never the
    show. Never inside the project folder, where a downloaded copy of the
    music would travel to whoever the project is next handed to.
    """
    return cache_root() / "live"


def human_bytes(size: int) -> str:
    """A size a person can act on, not a number to be parsed."""
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}".replace(".0 ", " ")
        value /= 1024.0
    return f"{value:.1f} GB"


@dataclass(frozen=True)
class ShowStore:
    """One show's downloaded data on this machine."""

    show_id: str

    @property
    def root(self) -> Path:
        return live_root() / self.show_id

    @property
    def media_dir(self) -> Path:
        return self.root / _MEDIA_DIR

    @property
    def peaks_dir(self) -> Path:
        return self.root / _PEAKS_DIR

    @property
    def document_path(self) -> Path:
        return self.root / _DOCUMENT

    @property
    def journal_path(self) -> Path:
        """The append-only operation log. ``document.json`` stays reserved
        for a compacted snapshot, which is a different artefact with a
        different lifetime — a journal is never rewritten, a snapshot is."""
        return self.root / _JOURNAL

    def ensure(self) -> Path:
        """Create the directories. Safe to call repeatedly."""
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.peaks_dir.mkdir(parents=True, exist_ok=True)
        return self.root

    def exists(self) -> bool:
        return self.root.is_dir()

    def size_bytes(self) -> int:
        """Total bytes on disk, or 0 when nothing has been downloaded.

        Walked rather than tracked in a counter: a counter drifts the first
        time a download is interrupted, and this is the number a person uses
        to decide whether to delete gigabytes.
        """
        root = self.root
        if not root.is_dir():
            return 0

        total = 0
        for entry in root.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                # A file that vanished mid-walk is not an error worth raising
                # to somebody who only asked how big a folder is.
                continue
        return total

    def media_count(self) -> int:
        directory = self.media_dir
        if not directory.is_dir():
            return 0
        try:
            return sum(1 for entry in directory.iterdir() if entry.is_file())
        except OSError:
            return 0

    def delete(self) -> bool:
        """Remove everything downloaded for this show. Returns whether it went.

        Refuses to touch anything outside the live root — the show id arrives
        from a settings file that a person can edit, and a path traversal in
        it must not turn "clear cached music" into deleting a home directory.
        """
        root = self.root
        try:
            resolved = root.resolve()
            parent = live_root().resolve()
        except OSError:
            return False

        if resolved == parent or parent not in resolved.parents:
            return False

        if not resolved.is_dir():
            return False

        shutil.rmtree(resolved, ignore_errors=True)
        return not resolved.exists()


def store_for(show_id: str) -> ShowStore | None:
    """A store for ``show_id``, or None when the id could not name a folder.

    Ids come from the host and from a settings file, so anything with a path
    separator, a parent reference, or nothing in it at all is refused here
    rather than at the moment somebody presses Delete.
    """
    cleaned = str(show_id or "").strip()
    if not cleaned or cleaned in {".", ".."}:
        return None
    if any(character in cleaned for character in "/\\:"):
        return None
    return ShowStore(show_id=cleaned)
