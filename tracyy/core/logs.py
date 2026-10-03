"""Session logging.

V1 swallowed almost every failure with a bare ``except Exception: pass``, so a
device that stopped responding or a project that failed to save left no trace
at all. V2 keeps the same non-fatal behaviour — a show must not stop because a
soundcard disappeared — but writes what happened to a rotating session log
next to the user's projects, and to stderr while running from source.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from .paths import data_root, is_frozen

__all__ = ["configure", "get_logger", "log_path"]

_CONFIGURED = False
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-24s %(message)s"


def log_path() -> Path:
    directory = data_root() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "tracyy.log"


def configure(level: int | str | None = None) -> None:
    """Install the root handlers. Safe to call more than once."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    if level is None:
        raw = str(os.environ.get("TRACYY_LOG_LEVEL", "")).strip().upper()
        level = raw or ("INFO" if is_frozen() else "DEBUG")

    root = logging.getLogger("tracyy")
    root.setLevel(level)
    root.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_path(),
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # A read-only or missing documents folder must never block startup.
        pass

    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Logger for a Tracyy module. ``name`` is usually ``__name__``."""
    configure()
    if not name.startswith("tracyy"):
        name = f"tracyy.{name}"
    return logging.getLogger(name)
