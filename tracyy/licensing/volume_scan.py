"""Volume probing that survives an unresponsive mount.

A stale network mount — the file server is gone but the mount point is still
in the kernel's table — does not raise from ``is_dir()`` or ``iterdir()``. It
blocks, with no timeout, until the mount is forcibly unmounted. The Admin USB
scan walks every mounted volume looking for a dongle, so one dead share used
to wedge Tracyy before the main window was ever built.

Two defences live here, and the USB scan uses both.

``is_network_path`` answers "which filesystem is this mount point on?" from the
kernel's cached mount table, without touching the mount itself. An Admin USB
dongle is never on SMB, NFS or AFP, so those roots are skipped outright.

``run_with_deadline`` runs whatever blocking work is left on a worker thread
and stops waiting once a deadline passes, reporting ``PENDING``. The worker is
never abandoned: it keeps running and a later call collects its result, so a
volume that is merely slow is found on a later poll instead of being reported
as absent. A volume that never answers parks exactly one thread for the life
of the process — not one thread per poll.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

__all__ = [
    "NETWORK_FILESYSTEMS",
    "PENDING",
    "PROBE_SECONDS",
    "SCAN_BUDGET_SECONDS",
    "forget_completed",
    "is_network_path",
    "mount_table",
    "reset",
    "run_with_deadline",
]

#: How long one volume may take to answer before the scan moves on.
PROBE_SECONDS = 1.5

#: Ceiling for a whole scan, however many volumes are mounted. Startup runs
#: this on the GUI thread, so the total wait has to stay bounded.
SCAN_BUDGET_SECONDS = 3.0

#: A finished probe left uncollected for longer than this is discarded and the
#: work is run again, so a caller never acts on an arbitrarily old answer.
RESULT_MAX_AGE_SECONDS = 10.0

_MOUNT_TABLE_TTL_SECONDS = 5.0

#: Nothing here can be a USB mass-storage device.
NETWORK_FILESYSTEMS = frozenset(
    {
        "9p",
        "afpfs",
        "afs",
        "beegfs",
        "cephfs",
        "cifs",
        "coda",
        "davfs",
        "davfs2",
        "ftpfs",
        "fuse.sshfs",
        "gfs2",
        "glusterfs",
        "lustre",
        "ncpfs",
        "nfs",
        "nfs4",
        "smb3",
        "smbfs",
        "sshfs",
        "webdav",
    }
)


class _Pending:
    """Result of a probe that has not answered inside its deadline."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "PENDING"


#: Returned by :func:`run_with_deadline` when the deadline passed. Distinct
#: from ``None`` and from ``[]``: it means "not known yet", never "nothing".
PENDING = _Pending()


# -- mount table ---------------------------------------------------------

_mount_lock = threading.Lock()
_mount_cache: tuple[float, tuple[tuple[str, str], ...]] | None = None

_MOUNT_LINE = re.compile(r"^(?P<source>.*?) on (?P<point>.*) \((?P<options>.*)\)$")

_MFSTYPENAMELEN = 16
_MAXPATHLEN = 1024
_MNT_NOWAIT = 2

_getfsstat: Any = None
_StatFS: Any = None


def _load_macos_getfsstat() -> tuple[Any, Any]:
    """Bind ``getfsstat`` and the ``struct statfs`` layout it fills in."""
    global _getfsstat, _StatFS
    if _getfsstat is not None:
        return _getfsstat, _StatFS

    import ctypes
    import ctypes.util

    class StatFS(ctypes.Structure):
        _fields_ = (
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", ctypes.c_int32 * 2),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * _MFSTYPENAMELEN),
            ("f_mntonname", ctypes.c_char * _MAXPATHLEN),
            ("f_mntfromname", ctypes.c_char * _MAXPATHLEN),
            ("f_flags_ext", ctypes.c_uint32),
            ("f_reserved", ctypes.c_uint32 * 7),
        )

    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    # x86_64 keeps the pre-64-bit-inode symbol as the default; arm64 has only
    # the 64-bit form, under the plain name.
    entry = None
    for name in ("getfsstat$INODE64", "getfsstat64", "getfsstat"):
        try:
            entry = getattr(libc, name)
        except AttributeError:
            continue
        break
    if entry is None:
        raise OSError("getfsstat is unavailable")

    entry.restype = ctypes.c_int
    entry.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
    _getfsstat, _StatFS = entry, StatFS
    return _getfsstat, _StatFS


def _macos_mount_table() -> list[tuple[str, str]]:
    """Mount points and filesystem types straight from the kernel.

    ``MNT_NOWAIT`` returns each mount's cached ``statfs``, so this never asks
    a file server anything and cannot block on one that has gone away.
    """
    import ctypes

    entry, struct_type = _load_macos_getfsstat()
    count = entry(None, 0, _MNT_NOWAIT)
    if count <= 0:
        return []

    # Mounts can appear between the sizing call and the read.
    buffer = (struct_type * (count + 8))()
    filled = entry(ctypes.byref(buffer), ctypes.sizeof(buffer), _MNT_NOWAIT)
    if filled <= 0:
        return []

    table: list[tuple[str, str]] = []
    for index in range(min(filled, len(buffer))):
        record = buffer[index]
        point = record.f_mntonname.decode("utf-8", errors="replace")
        fstype = record.f_fstypename.decode("utf-8", errors="replace")
        if point:
            table.append((point, fstype.lower()))
    return table


def _mount_command_table() -> list[tuple[str, str]]:
    """Fallback for macOS: parse ``mount(8)``, which also uses ``MNT_NOWAIT``."""
    output = subprocess.run(
        ["/sbin/mount"],
        capture_output=True,
        timeout=5,
        check=False,
    ).stdout.decode("utf-8", errors="replace")

    table: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = _MOUNT_LINE.match(line.strip())
        if match is None:
            continue
        point = match.group("point").strip()
        fstype = match.group("options").split(",")[0].strip().lower()
        if point and fstype:
            table.append((point, fstype))
    return table


def _unescape_mountinfo(value: str) -> str:
    # /proc escapes space, tab, newline and backslash as octal.
    for escape, character in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(escape, character)
    return value


def _linux_mount_table() -> list[tuple[str, str]]:
    """Mount points and types from procfs, which never blocks on a dead server."""
    with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
        raw = handle.read()

    table: list[tuple[str, str]] = []
    for line in raw.splitlines():
        head, separator, tail = line.partition(" - ")
        if not separator:
            continue
        head_fields = head.split(" ")
        tail_fields = tail.split(" ")
        if len(head_fields) < 5 or not tail_fields:
            continue
        point = _unescape_mountinfo(head_fields[4])
        fstype = tail_fields[0].strip().lower()
        if point and fstype:
            table.append((point, fstype))
    return table


def mount_table() -> tuple[tuple[str, str], ...]:
    """``(mount point, filesystem type)`` for every mount, briefly cached.

    Empty when the platform has no readable table; callers must treat that as
    "unknown", never as "nothing is a network mount".
    """
    global _mount_cache

    now = time.monotonic()
    with _mount_lock:
        cached = _mount_cache
        if cached is not None and now - cached[0] < _MOUNT_TABLE_TTL_SECONDS:
            return cached[1]

    system = platform.system().lower()
    table: list[tuple[str, str]] = []
    if system == "darwin":
        for reader in (_macos_mount_table, _mount_command_table):
            try:
                table = reader()
            except Exception:
                table = []
            if table:
                break
    elif system not in {"windows", ""}:
        try:
            table = _linux_mount_table()
        except Exception:
            table = []

    frozen = tuple(table)
    with _mount_lock:
        _mount_cache = (now, frozen)
    return frozen


def is_network_path(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` sits on a network filesystem.

    Answered from the mount table alone. Nothing here touches ``path`` — not
    even ``resolve()``, which is itself one of the calls that hangs on a stale
    mount. Unknown paths come back ``False``, so a missing mount table only
    costs the shortcut, never a real dongle.
    """
    target = os.path.normpath(os.fspath(path))
    best_point = ""
    best_type = ""
    for point, fstype in mount_table():
        normalized = os.path.normpath(point)
        under = target == normalized or target.startswith(normalized.rstrip(os.sep) + os.sep)
        if under and len(normalized) >= len(best_point):
            best_point, best_type = normalized, fstype
    return best_type in NETWORK_FILESYSTEMS


# -- deadline probe ------------------------------------------------------


class _Job:
    __slots__ = ("done", "error", "finished_at", "value")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None
        self.finished_at = 0.0


_jobs_lock = threading.Lock()
_jobs: dict[str, _Job] = {}


def _work(job: _Job, func: Callable[[], Any]) -> None:
    try:
        job.value = func()
    except BaseException as exc:  # handed back to the caller, not swallowed
        job.error = exc
    finally:
        job.finished_at = time.monotonic()
        job.done.set()


def _collect(job: _Job) -> Any:
    if job.error is not None:
        raise job.error
    return job.value


def run_with_deadline(
    key: str,
    func: Callable[[], Any],
    timeout: float | None = None,
) -> Any:
    """Run ``func`` on a worker thread, waiting at most ``timeout`` seconds.

    Returns whatever ``func`` returned, re-raises whatever it raised, or
    returns :data:`PENDING` if the deadline passed first.

    A pending worker is left running rather than abandoned, and ``key`` keeps
    a second one from being started behind it. That is what bounds the cost of
    a mount that never answers: one parked thread, and every later call for
    the same key returns ``PENDING`` immediately instead of spending another
    deadline on it. When the mount recovers, the parked worker finishes and
    the next call collects its result.
    """
    if timeout is None:
        timeout = PROBE_SECONDS

    with _jobs_lock:
        job = _jobs.get(key)
        if job is not None:
            if not job.done.is_set():
                return PENDING
            del _jobs[key]
            if time.monotonic() - job.finished_at <= RESULT_MAX_AGE_SECONDS:
                return _collect(job)
            # Answered, but too long ago to act on. Run it again.

        job = _Job()
        _jobs[key] = job
        threading.Thread(
            target=_work,
            args=(job, func),
            name="TracyyVolumeProbe",
            daemon=True,
        ).start()

    if not job.done.wait(timeout):
        return PENDING

    with _jobs_lock:
        if _jobs.get(key) is job:
            del _jobs[key]
    return _collect(job)


def forget_completed() -> None:
    """Drop collected results so the next call probes afresh.

    Workers still in flight stay tracked. Dropping those would start a second
    thread on a mount that is already known not to answer.
    """
    global _mount_cache

    with _jobs_lock:
        for key in [key for key, job in _jobs.items() if job.done.is_set()]:
            del _jobs[key]
    with _mount_lock:
        _mount_cache = None


def reset() -> None:
    """Forget everything, including in-flight workers. For tests only."""
    global _mount_cache

    with _jobs_lock:
        _jobs.clear()
    with _mount_lock:
        _mount_cache = None


_DRIVE_REMOTE = 4


def windows_remote_drive_letters() -> frozenset[str]:
    """Drive letters that are mapped network shares.

    ``GetDriveTypeW`` answers from what the mapping already recorded, so a
    drive whose server has gone away is classified without waiting on it —
    unlike ``exists()``, which blocks on exactly that drive. Returns an empty
    set off Windows, or if the call is unavailable.
    """
    if platform.system().lower() != "windows":
        return frozenset()
    try:
        import ctypes

        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW  # type: ignore[attr-defined]
    except Exception:
        return frozenset()

    remote: set[str] = set()
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        try:
            if int(get_drive_type(f"{letter}:\\")) == _DRIVE_REMOTE:
                remote.add(letter)
        except Exception:
            continue
    return frozenset(remote)
