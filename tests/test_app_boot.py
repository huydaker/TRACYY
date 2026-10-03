"""The window builds, and every periodic job actually runs.

This is the regression test the V1 refactor most needed: the main window is
assembled from nine controller mixins, so a rename in any of them only shows
up when the application is started. Booting it offscreen catches that in a
second instead of at a load-in.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

# Must be set before QApplication is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer

from tracyy.core.perf import ActivityState

#: Long enough for the background tier (1 s) to fire at least twice.
RUN_MILLISECONDS = 2600


@pytest.fixture(scope="module")
def booted():
    from tracyy.ui.main_window import MainWindow, TracyyApplication
    from tracyy.ui.theme import apply_theme

    app = TracyyApplication.instance() or TracyyApplication([])
    apply_theme(app)
    window = MainWindow()
    window.show()

    captured: dict[str, object] = {}

    def finish() -> None:
        captured["statistics"] = window.tick_scheduler.statistics()
        app.quit()

    QTimer.singleShot(RUN_MILLISECONDS, finish)
    app.exec()

    yield window, captured["statistics"]

    window.close()


def test_theme_is_applied(booted) -> None:
    from tracyy.ui.theme import TRACYY_QSS

    assert "QWidget" in TRACYY_QSS
    assert len(TRACYY_QSS) > 10_000, "the full style sheet should survive extraction"


def test_every_scheduled_job_ran(booted) -> None:
    _window, statistics = booted
    jobs = {job["name"]: job for job in statistics["jobs"]}

    expected = {
        "playhead",
        "labels",
        "server_snapshot",
        "usb_admin",
        "license_countdown",
        "license_connectivity",
        "activity",
    }
    assert expected <= set(jobs)

    never_ran = [name for name, job in jobs.items() if job["calls"] == 0]
    assert not never_ran, f"registered but never dispatched: {never_ran}"

    disabled = [name for name, job in jobs.items() if not job["enabled"]]
    assert not disabled, f"retired after repeated failures: {disabled}"


def test_tiers_run_at_different_rates(booted) -> None:
    """The whole point of the scheduler: slower work must run less often."""
    _window, statistics = booted
    jobs = {job["name"]: job for job in statistics["jobs"]}
    assert jobs["playhead"]["calls"] > jobs["labels"]["calls"]
    assert jobs["labels"]["calls"] > jobs["license_countdown"]["calls"]


def test_idle_window_is_cheap(booted) -> None:
    _window, statistics = booted
    assert statistics["state"] == ActivityState.IDLE.value
    # V1 spent ~16 ms of every second redrawing an idle window. Anything near
    # that here means the tiering regressed.
    assert statistics["load"] < 0.05, statistics


def test_refresh_position_still_works(booted) -> None:
    """Seeks and loads force a full refresh through this one call."""
    window, _statistics = booted
    window.refresh_position()
