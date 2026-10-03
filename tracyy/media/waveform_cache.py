"""On-disk cache of analysed waveform envelopes.

Analysing a full track means reading every sample off disk and decoding it,
which is why Tracyy does it once and keeps the result. V1 had this logic
inline in the analysis worker, with its own copy of the per-platform cache
directory rules and no way to ever reclaim the space: every version of every
file the operator ever opened stayed on disk forever.

V2 puts it here, behind one object, resolves the directory through
:mod:`tracyy.core.paths` like everything else, and adds :meth:`WaveformCache.prune`
so the cache has a ceiling.

Imported by spawned worker processes — no Qt, no sounddevice.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import numpy as np

from tracyy.core.paths import cache_root

__all__ = ["WaveformCache", "default_cache"]

#: Bump whenever the analyser's output changes shape or meaning, so a preview
#: written by an older build can never be reused.
CACHE_VERSION = "v2-fulltrack-peaks-1"

DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_AGE_DAYS = 90


class WaveformCache:
    """Content-addressed store of float32 envelopes, one file per entry."""

    def __init__(self, root: Path | None = None, *, version: str = CACHE_VERSION) -> None:
        self._root = Path(root) if root is not None else cache_root() / "waveforms"
        self._version = str(version)

    @property
    def root(self) -> Path:
        return self._root

    # -- addressing ------------------------------------------------------

    def path_for(self, source: str | Path, points: int) -> Path | None:
        """Cache file for one (source, resolution) pair, or None if unusable.

        The key includes the file's size and modification time, so replacing a
        track with a different edit at the same path invalidates the entry
        without anybody having to remember to clear the cache.
        """
        try:
            resolved = Path(source).resolve()
            stat = resolved.stat()
        except OSError:
            return None

        identity = "\n".join(
            (
                self._version,
                str(resolved),
                str(int(stat.st_size)),
                str(int(stat.st_mtime_ns)),
                str(int(points)),
            )
        )
        digest = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()
        return self._root / f"{digest}.npy"

    # -- access ----------------------------------------------------------

    def load(self, source: str | Path, points: int) -> np.ndarray | None:
        cache_path = self.path_for(source, points)
        if cache_path is None or not cache_path.is_file():
            return None
        try:
            array = np.load(cache_path, allow_pickle=False)
            preview = np.asarray(array, dtype=np.float32).reshape(-1)
            if preview.size < 2 or not np.all(np.isfinite(preview)):
                return None
        except (OSError, ValueError, EOFError):
            # Truncated or written by a different numpy; drop it and re-analyse.
            try:
                cache_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

        # Refresh the access time so pruning keeps what is actually in use.
        try:
            now = time.time()
            os.utime(cache_path, (now, cache_path.stat().st_mtime))
        except OSError:
            pass
        return np.ascontiguousarray(preview, dtype=np.float32)

    def store(self, source: str | Path, points: int, preview: np.ndarray) -> bool:
        cache_path = self.path_for(source, points)
        if cache_path is None:
            return False

        temporary: Path | None = None
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename: a crash mid-write must not leave a half file
            # that the next run would load as a valid envelope. The temporary
            # name has to end in .npy — np.save appends the extension itself
            # otherwise, and the rename would then miss the file it wrote.
            temporary = cache_path.with_name(f"{cache_path.stem}.{os.getpid()}.tmp.npy")
            np.save(temporary, np.asarray(preview, dtype=np.float32), allow_pickle=False)
            os.replace(temporary, cache_path)
            return True
        except (OSError, ValueError):
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    # -- housekeeping ----------------------------------------------------

    def entries(self) -> list[tuple[Path, int, float]]:
        """``(path, size, last access)`` for every cached envelope."""
        found: list[tuple[Path, int, float]] = []
        try:
            for candidate in self._root.glob("*.npy"):
                try:
                    stat = candidate.stat()
                except OSError:
                    continue
                found.append((candidate, int(stat.st_size), float(stat.st_atime)))
        except OSError:
            return []
        return found

    def total_bytes(self) -> int:
        return sum(size for _path, size, _atime in self.entries())

    def prune(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    ) -> int:
        """Delete stale entries. Returns how many files were removed.

        Age is checked first, then the least recently used entries go until
        the cache fits. Safe to call while other processes read the cache: a
        deleted entry simply re-analyses.
        """
        entries = self.entries()
        if not entries:
            return 0

        removed = 0
        cutoff = time.time() - max(0.0, float(max_age_days)) * 86400.0
        survivors: list[tuple[Path, int, float]] = []
        for path, size, atime in entries:
            if atime < cutoff:
                if _unlink(path):
                    removed += 1
                continue
            survivors.append((path, size, atime))

        total = sum(size for _path, size, _atime in survivors)
        if total <= max_bytes:
            return removed

        survivors.sort(key=lambda item: item[2])  # oldest access first
        for path, size, _atime in survivors:
            if total <= max_bytes:
                break
            if _unlink(path):
                removed += 1
                total -= size
        return removed

    def clear(self) -> int:
        removed = 0
        for path, _size, _atime in self.entries():
            if _unlink(path):
                removed += 1
        return removed


def _unlink(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


#: Shared instance. Worker processes and the GUI address the same directory.
#: Named ``default_cache`` rather than ``waveform_cache`` so it cannot shadow
#: this module when the package re-exports it.
default_cache = WaveformCache()
