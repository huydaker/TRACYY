"""Filesystem roots for source runs and frozen (PyInstaller) builds.

V1 resolved these ad hoc: every module counted its own ``__file__.parents[n]``,
so moving a file silently moved the config, cache or icon folder with it. V2
resolves them once, here, and every other module asks this module.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

__all__ = [
    "app_root",
    "bundle_dir",
    "cache_root",
    "data_root",
    "icon_dir",
    "is_frozen",
    "resource_root",
    "source_root",
]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


@functools.cache
def source_root() -> Path:
    """The distributed source folder, i.e. the parent of the ``tracyy`` package."""
    return Path(__file__).resolve().parents[2]


@functools.cache
def bundle_dir() -> Path | None:
    """PyInstaller's temporary extraction directory, when running frozen."""
    raw = getattr(sys, "_MEIPASS", None)
    return Path(raw).resolve() if raw else None


@functools.cache
def resource_root() -> Path:
    """Read-only assets: icons, QML, bundled web files."""
    if is_frozen():
        return bundle_dir() or Path(sys.executable).resolve().parent
    return source_root()


@functools.cache
def app_root() -> Path:
    """Writable application root — beside the executable, or the source folder."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return source_root()


@functools.cache
def data_root() -> Path:
    """User documents/projects root. Created on first use."""
    override = str(os.environ.get("TRACYY_DATA_ROOT", "")).strip()
    if override:
        root = Path(override).expanduser().resolve()
    elif is_frozen():
        root = Path.home() / "Documents" / "Tracyy"
    else:
        # Keep V1's layout: projects live beside the application folder.
        root = source_root().parent
    root.mkdir(parents=True, exist_ok=True)
    return root


@functools.cache
def cache_root() -> Path:
    """Machine-local cache (waveform peaks, thumbnails). Safe to delete."""
    override = str(os.environ.get("TRACYY_CACHE_ROOT", "")).strip()
    if override:
        root = Path(override).expanduser().resolve()
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        root = Path(base) / "Tracyy" / "cache"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "Tracyy"
    else:
        base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
        root = Path(base) / "tracyy"
    root.mkdir(parents=True, exist_ok=True)
    return root


def icon_dir() -> Path:
    return resource_root() / "icon"
