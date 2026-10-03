"""Modules that worker processes import must not drag in Qt.

Tracyy spawns a decoder process per source, a pool of waveform analysers, and
the LAN cue viewer. Every one of them imports part of the package. If a
convenience re-export quietly pulls PySide6 into those modules, each process
pays several hundred milliseconds of import time and tens of megabytes of RAM
that it never uses — and nothing visibly breaks, so it stays that way.

Each check runs in a fresh interpreter: the test session itself imports Qt.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PROBE = """
import sys
import {module}
heavy = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in {{"PySide6", "shiboken6", "shibokensupport"}}
)
print(",".join(heavy))
"""

#: Imported by spawned processes; must stay free of the GUI stack.
WORKER_MODULES = [
    "tracyy",
    "tracyy.core.paths",
    "tracyy.core.logs",
    "tracyy.audio",
    "tracyy.audio.streaming",
    "tracyy.audio.resampler",
    "tracyy.audio.ringbuffer",
    "tracyy.audio.ltc_encoder",
    "tracyy.media.waveform_cache",
    "tracyy.workers",
    "tracyy.workers.media_tasks",
    "tracyy.server.cue_viewer",
]


def _qt_modules_after_importing(module: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(module=module)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"import {module} failed:\n{result.stderr}"
    output = result.stdout.strip()
    return output.split(",") if output else []


@pytest.mark.parametrize("module", WORKER_MODULES)
def test_worker_module_does_not_import_qt(module: str) -> None:
    loaded = _qt_modules_after_importing(module)
    assert not loaded, f"{module} pulled in Qt: {loaded}"


def test_cue_viewer_does_not_import_numpy() -> None:
    """The LAN viewer ships as its own small executable; keep it that way."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, tracyy.server.cue_viewer; "
            "print('numpy' in sys.modules, 'av' in sys.modules)",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False"


def test_launcher_stays_light() -> None:
    """``main.py`` is re-imported as __mp_main__ by every spawned process."""
    loaded = _qt_modules_after_importing("main")
    assert not loaded, f"main.py pulled in Qt at import time: {loaded}"
