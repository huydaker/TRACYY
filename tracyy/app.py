"""Application bootstrap.

V1 put this in ``tracyy_app.main()`` together with nine hundred lines of
inline style sheet. V2 keeps the bootstrap here, the sheet in
:mod:`tracyy.ui.theme`, and the window in :mod:`tracyy.ui.main_window`.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QTimer

from tracyy import APP_NAME, __version__
from tracyy.core.logs import get_logger

__all__ = ["main"]

_log = get_logger(__name__)

#: Extensions Tracyy will open when handed a file by the OS.
PROJECT_SUFFIXES = {".tracyy", ".json"}


def _startup_project_paths(app) -> list[str]:
    """Project files to open, from argv and from macOS file-open events.

    Windows and Linux file associations pass the path as ``argv[1]``; macOS
    delivers a ``QFileOpenEvent`` instead, which ``TracyyApplication`` queues.
    """
    candidates: list[str] = []
    for raw_arg in sys.argv[1:]:
        candidate = Path(str(raw_arg)).expanduser()
        if candidate.is_file() and candidate.suffix.lower() in PROJECT_SUFFIXES:
            candidates.append(str(candidate))
    candidates.extend(app.pending_project_files())

    unique: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        normalized = str(Path(path).resolve())
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def _prune_caches() -> None:
    """Trim the waveform cache once per session, off the UI thread.

    V1 never reclaimed this space, so a machine that had opened a few hundred
    shows carried every envelope it had ever computed.
    """
    try:
        from tracyy.media.waveform_cache import default_cache

        removed = default_cache.prune()
        if removed:
            _log.info("pruned %d stale waveform cache entries", removed)
    except Exception:
        _log.warning("waveform cache prune failed", exc_info=True)


def main() -> None:
    started = time.perf_counter()

    # Imported here, not at module scope: this pulls in the whole Qt widget
    # stack, and ``python -m tracyy.app --help``-style entry points and the
    # decoder workers should never pay for it.
    from tracyy.ui.main_window import MainWindow, TracyyApplication
    from tracyy.ui.theme import apply_theme

    app = TracyyApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    apply_theme(app)

    window = MainWindow()
    app.project_file_requested.connect(window.load_project_path)
    window.show()

    for project_path in _startup_project_paths(app):
        QTimer.singleShot(0, lambda current=project_path: window.load_project_path(current))

    threading.Thread(target=_prune_caches, name="TracyyCachePrune", daemon=True).start()

    _log.info(
        "%s %s ready in %.0f ms",
        APP_NAME,
        __version__,
        (time.perf_counter() - started) * 1000.0,
    )
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
