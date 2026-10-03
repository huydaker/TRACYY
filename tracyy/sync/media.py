"""Getting somebody else's music onto this machine's own disk.

A show is a pile of audio files that exist at one path on the host and at no
path at all on a laptop that just joined. This module is the part that closes
that gap: what the host publishes about each track, how a client asks for the
bytes, and — the half that actually matters — how a file that arrived over an
unreliable link is only ever used once it is provably the same file.

Two rules everything here is built around:

*Content is the identity, not the filename.* Two shows both containing
``intro.mp3`` are two different songs, and the same song renamed is the same
song. Every entry carries a hash, and a download that does not match it is
thrown away rather than played.

*A file is either absent or complete.* Bytes land in a ``.part`` file and are
renamed into place in one atomic step after verification, so a power cut or a
closed laptop leaves a resumable fragment — never a half file that opens,
plays, and stops early in the middle of a show.

Deliberately free of Qt and of sockets. The server and the client own the
transport; this owns the disk and the arithmetic, so both ends agree without
either importing the other.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "MAX_MEDIA_BYTES",
    "MEDIA_CHUNK_BYTES",
    "MediaDownloader",
    "MediaEntry",
    "MediaLibrary",
    "MediaTarget",
    "hash_file",
]

#: Bytes per request. Big enough that a show's worth of music does not become
#: thousands of round trips, small enough that one lost reply costs a moment
#: rather than a restart, and that a JSON body stays a body rather than a
#: memory event on a laptop that is also decoding audio.
MEDIA_CHUNK_BYTES = 256 * 1024

#: A ceiling on what one entry may claim to be. The size arrives from another
#: machine and is used to allocate and to decide when a download is done; a
#: hostile or broken host must not be able to talk this one into filling its
#: disk. Two gigabytes is far past any real show file.
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024

#: How much of a file to read at a time when hashing. Hashing runs off the UI
#: and audio threads, but it still shares the disk with playback.
_HASH_CHUNK = 1024 * 1024


def hash_file(path: str | Path) -> str:
    """The content hash of a file, or an empty string when it cannot be read.

    SHA-256 over the whole file rather than a sample of it: a sampled hash
    makes two different masters of the same song look identical, which is
    precisely the mistake this exists to prevent.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(_HASH_CHUNK)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


@dataclass(frozen=True)
class MediaEntry:
    """One track as the host describes it to everybody else.

    ``name`` and ``suffix`` are for humans and for opening the file; nothing
    is ever matched on them. ``track_id`` says which song this is inside the
    session, ``media_hash`` says which bytes.
    """

    track_id: str
    name: str
    size: int
    media_hash: str
    suffix: str = ""

    def to_wire(self) -> dict:
        return {
            "track_id": self.track_id,
            "name": self.name,
            "size": int(self.size),
            "media_hash": self.media_hash,
            "suffix": self.suffix,
        }

    @classmethod
    def from_wire(cls, payload: dict) -> MediaEntry | None:
        """Rebuild an entry, or None when it could not have come from a track.

        Everything here arrives from another machine. An entry without a hash
        or with an impossible size is refused now, rather than after a laptop
        has spent ten minutes downloading it.
        """
        if not isinstance(payload, dict):
            return None
        track_id = str(payload.get("track_id", "") or "").strip()
        media_hash = str(payload.get("media_hash", "") or "").strip().lower()
        try:
            size = int(payload.get("size"))
        except (TypeError, ValueError):
            return None
        if not track_id or len(media_hash) != 64 or not size or size > MAX_MEDIA_BYTES:
            return None
        if size < 0:
            return None

        suffix = str(payload.get("suffix", "") or "")
        # A suffix is pasted onto a filename this machine then writes, so it
        # may be an extension and nothing else — no separators, no parents.
        if suffix and (len(suffix) > 12 or not suffix.startswith(".") or not suffix[1:].isalnum()):
            suffix = ""
        return cls(
            track_id=track_id,
            name=str(payload.get("name", "") or "")[:200],
            size=size,
            media_hash=media_hash,
            suffix=suffix,
        )


class MediaLibrary:
    """What the host is willing to hand out, and nothing else.

    An explicit map from track id to path, rather than a directory to serve:
    a request names a track in the session, never a path, so there is no
    filename for a client to walk out of. A path that is not in here cannot be
    read no matter what arrives on the socket.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._paths: dict[str, str] = {}
        #: (path, size, mtime) -> hash. Hashing a show's worth of music takes
        #: seconds, and the manifest is asked for on every join.
        self._hashes: dict[tuple[str, int, int], str] = {}

    def publish(self, tracks: dict[str, str]) -> None:
        """Replace the set of tracks on offer. ``track_id -> local path``."""
        with self._lock:
            self._paths = {
                str(track_id): str(path)
                for track_id, path in (tracks or {}).items()
                if str(track_id) and str(path)
            }

    def path_for(self, track_id: str) -> str:
        with self._lock:
            return self._paths.get(str(track_id), "")

    def manifest(self) -> list[dict]:
        """Every track this host can serve, with its hash.

        Files that have gone missing since the project was opened are simply
        left out: a client cannot download them, and an entry it can never
        satisfy would leave it retrying for the rest of the show.
        """
        with self._lock:
            paths = dict(self._paths)

        entries = []
        for track_id, path in paths.items():
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if not stat.st_size or stat.st_size > MAX_MEDIA_BYTES:
                continue

            key = (path, int(stat.st_size), int(stat.st_mtime_ns))
            with self._lock:
                media_hash = self._hashes.get(key, "")
            if not media_hash:
                media_hash = hash_file(path)
                if not media_hash:
                    continue
                with self._lock:
                    self._hashes[key] = media_hash

            name = Path(path).name
            entries.append(
                MediaEntry(
                    track_id=track_id,
                    name=name,
                    size=int(stat.st_size),
                    media_hash=media_hash,
                    suffix=Path(path).suffix.lower(),
                ).to_wire()
            )
        return entries

    def read_chunk(self, track_id: str, offset: int, length: int) -> bytes:
        """``length`` bytes from ``offset``, or empty when there is nothing there.

        Raises nothing on a bad offset: a client resuming from a stale idea of
        how much it had is a normal event, and the empty answer plus the size
        in the manifest is enough for it to work out what happened.
        """
        path = self.path_for(track_id)
        if not path:
            return b""
        try:
            start = max(0, int(offset))
            want = max(0, min(int(length), MEDIA_CHUNK_BYTES))
        except (TypeError, ValueError):
            return b""
        if not want:
            return b""
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                return handle.read(want)
        except OSError:
            return b""


#: Characters a filename may not contain on the platforms Tracyy runs on,
#: plus the ones that would make a name argue with a shell later.
_UNSAFE_NAME = set(r'<>:"/\|?*') | {chr(code) for code in range(32)}


def _safe_filename(entry: MediaEntry) -> str:
    """What to call this track on disk: its own name, made safe to write.

    The host's filename is used because a person opening the folder should
    recognise their show, but it is never trusted as a path — separators,
    parent references and control characters go, and the track id is appended
    so that two different songs called ``intro.mp3`` cannot land on each
    other. The extension comes from the already-validated suffix.
    """
    stem = Path(str(entry.name or "")).stem
    cleaned = "".join(character for character in stem if character not in _UNSAFE_NAME)
    cleaned = cleaned.strip(" .")[:80]
    if not cleaned:
        cleaned = "track"
    return f"{cleaned} [{entry.track_id}]{entry.suffix}"


class MediaTarget:
    """One file being written on the receiving machine.

    Holds the ``.part`` while it fills and performs the one step that makes
    the file real. Nothing outside this class ever names the final path, so
    there is no way to end up reading a file that was never verified.
    """

    def __init__(self, media_dir: str | Path, entry: MediaEntry) -> None:
        self.entry = entry
        self._dir = Path(media_dir)
        self.final_path = self._dir / _safe_filename(entry)
        self.part_path = self.final_path.with_name(self.final_path.name + ".part")

    @property
    def complete(self) -> bool:
        """Whether the finished file is already here and is the right one.

        The size is checked but not the hash: hashing every track on every
        join would cost seconds of disk on a laptop that is about to play a
        show, and the file only got its name by passing a hash check.
        """
        try:
            return self.final_path.stat().st_size == self.entry.size
        except OSError:
            return False

    @property
    def received(self) -> int:
        """How many bytes of the fragment are already on disk."""
        try:
            return int(self.part_path.stat().st_size)
        except OSError:
            return 0

    def open_for_append(self):
        self._dir.mkdir(parents=True, exist_ok=True)
        return open(self.part_path, "ab")

    def discard(self) -> None:
        """Throw the fragment away, for when it turns out to be the wrong file."""
        try:
            self.part_path.unlink(missing_ok=True)
        except OSError:
            pass

    def finish(self) -> str:
        """Verify the fragment and put it in place. Returns the path, or ''.

        Size first because it is free, then the hash, and only then the
        rename. A fragment that fails either check is deleted rather than
        left to be resumed forever: whatever is on the far end, it is not what
        this entry says it is.
        """
        if self.received != self.entry.size:
            return ""
        if hash_file(self.part_path) != self.entry.media_hash:
            self.discard()
            return ""
        try:
            os.replace(self.part_path, self.final_path)
        except OSError:
            return ""
        return str(self.final_path)


class MediaDownloader:
    """Pulls whole tracks in the background, one at a time, resuming as it goes.

    One at a time on purpose: a show laptop is also decoding audio, and four
    parallel downloads on a contended Wi-Fi link finish no sooner while making
    every one of them slower to show progress for. The queue is replaced
    wholesale rather than appended to, so a client that reconnects to a
    changed show does not keep fetching a track that left the running order.

    Knows nothing about sockets: it is handed a ``fetch(track_id, offset)``
    that returns bytes, so the tests drive it without a server and the real
    thing drives it through the session's own authenticated request.
    """

    def __init__(
        self,
        media_dir: str | Path,
        fetch,
        manifest_fetch=None,
        chunk: int = MEDIA_CHUNK_BYTES,
    ) -> None:
        self._dir = Path(media_dir)
        self._fetch = fetch
        #: Asking for the manifest makes the host hash a show's worth of
        #: audio, so it happens on this thread too. Nothing that draws should
        #: ever wait for it.
        self._manifest_fetch = manifest_fetch
        self._want_manifest = False
        self._chunk = max(1, int(chunk))
        self._lock = threading.RLock()
        self._wanted: list[MediaEntry] = []
        self._completed: dict[str, str] = {}
        self._failed: dict[str, str] = {}
        self._active = ""
        self._received = 0
        self._size = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    # -- what the application calls --------------------------------------

    def refresh_manifest(self) -> None:
        """Ask the host again what the show contains, in the background."""
        self._want_manifest = True
        self._wake.set()

    def request(self, entries: list[MediaEntry]) -> None:
        """Set what should be on this machine. Already-complete files are skipped."""
        with self._lock:
            self._wanted = [entry for entry in entries if entry.track_id]
        self._wake.set()

    def known_entries(self) -> list[MediaEntry]:
        """What the last manifest said the show contains."""
        with self._lock:
            return list(self._wanted)

    def take_completed(self) -> dict[str, str]:
        """Tracks that finished since the last ask, as ``track_id -> path``."""
        with self._lock:
            done, self._completed = self._completed, {}
            return done

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "active": self._active,
                "received": self._received,
                "size": self._size,
                "waiting": len(self._wanted),
                "failed": dict(self._failed),
            }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tracyy-media", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    # -- the thread -------------------------------------------------------

    def _next_entry(self) -> MediaEntry | None:
        with self._lock:
            for entry in self._wanted:
                if entry.track_id in self._failed:
                    continue
                target = MediaTarget(self._dir, entry)
                if target.complete:
                    self._completed.setdefault(entry.track_id, str(target.final_path))
                    continue
                return entry
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._want_manifest and self._manifest_fetch is not None:
                self._want_manifest = False
                try:
                    entries = self._manifest_fetch() or []
                except Exception:  # pragma: no cover - defensive
                    entries = []
                if entries:
                    # Only replace the list when the answer was usable. A host
                    # that is briefly unreachable must not empty the queue and
                    # make a laptop forget what it was half way through.
                    self.request(entries)

            entry = self._next_entry()
            if entry is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            self._download(entry)

    def _download(self, entry: MediaEntry) -> None:
        target = MediaTarget(self._dir, entry)
        received = target.received
        if received > entry.size:
            # The fragment is longer than the file it claims to be. Whatever
            # produced it, resuming from here would only ever fail the hash.
            target.discard()
            received = 0

        with self._lock:
            self._active, self._received, self._size = entry.track_id, received, entry.size

        try:
            with target.open_for_append() as handle:
                while received < entry.size and not self._stop.is_set():
                    block = self._fetch(entry.track_id, received)
                    if not block:
                        # Nothing came back: the host has gone, or no longer
                        # has this track. Leave the fragment for the next
                        # attempt rather than starting again from zero.
                        self._note_failure(entry, "Máy chính không gửi được bài này.")
                        return
                    handle.write(block)
                    received += len(block)
                    with self._lock:
                        self._received = received
        except OSError as exc:
            self._note_failure(entry, f"Không ghi được xuống đĩa: {exc}")
            return

        if self._stop.is_set():
            return

        landed = target.finish()
        with self._lock:
            self._active, self._received, self._size = "", 0, 0
            if landed:
                self._completed[entry.track_id] = landed
                self._failed.pop(entry.track_id, None)
            else:
                self._failed[entry.track_id] = (
                    "Bài tải về không khớp với bản trên máy chính."
                )

    def _note_failure(self, entry: MediaEntry, message: str) -> None:
        with self._lock:
            self._failed[entry.track_id] = message
            self._active, self._received, self._size = "", 0, 0
