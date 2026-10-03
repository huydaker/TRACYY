"""Load management for the Tracyy V2 UI thread.

V1 drove the whole interface from one 15 ms ``PreciseTimer`` whose slot
rebuilt every label, the server snapshot and the cue lookups on all 66 ticks
per second, regardless of whether anything had changed, whether the transport
was even running, or whether the window was visible at all. On a show laptop
that is a permanent single-core load for output nobody is looking at.

V2 keeps one timer, but:

* work is registered into rate *tiers* and each tier fires at its own cadence;
* the cadence set is chosen by the current :class:`ActivityState`, so an idle
  or hidden window costs almost nothing;
* a :class:`LoadGovernor` measures how long the ticks actually take and backs
  every tier off when the machine cannot keep up, instead of queueing timer
  events until the UI stutters.

Nothing here is realtime-critical: audio output and LTC generation live in
their own callbacks and are never paced by this scheduler.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QObject, Qt, QTimer, Signal

__all__ = [
    "ActivityState",
    "LoadGovernor",
    "RateLimiter",
    "TickProfile",
    "Tier",
    "UiTickScheduler",
]


class Tier(str, Enum):
    """How urgently a piece of UI work needs to run."""

    #: Playhead, waveform progress, elapsed timecode — the only work a viewer
    #: can perceive as stuttering.
    PLAYHEAD = "playhead"
    #: Track title, durations, transport icon, cue highlight.
    LABELS = "labels"
    #: Web-viewer snapshot, follow-machine heartbeat, LAN publishing.
    NETWORK = "network"
    #: License countdown, device rescans, disk housekeeping.
    BACKGROUND = "background"


class ActivityState(str, Enum):
    PLAYING = "playing"
    #: Loaded and visible but not advancing.
    IDLE = "idle"
    #: Minimised, on another virtual desktop, or fully occluded.
    HIDDEN = "hidden"


@dataclass(frozen=True)
class TickProfile:
    """Interval in milliseconds for each tier, for one activity state."""

    playhead_ms: int
    labels_ms: int
    network_ms: int
    background_ms: int

    def interval_for(self, tier: Tier) -> int:
        return {
            Tier.PLAYHEAD: self.playhead_ms,
            Tier.LABELS: self.labels_ms,
            Tier.NETWORK: self.network_ms,
            Tier.BACKGROUND: self.background_ms,
        }[tier]

    def scaled(self, factor: float) -> TickProfile:
        factor = max(1.0, float(factor))
        return TickProfile(
            playhead_ms=int(self.playhead_ms * factor),
            labels_ms=int(self.labels_ms * factor),
            network_ms=int(self.network_ms * factor),
            background_ms=int(self.background_ms * factor),
        )


#: 60 Hz playhead is the ceiling: the waveform is the only widget that
#: benefits, and it redraws from a cached pixmap pyramid.
PROFILES: dict[ActivityState, TickProfile] = {
    ActivityState.PLAYING: TickProfile(
        playhead_ms=16,
        labels_ms=100,
        network_ms=200,
        background_ms=1000,
    ),
    ActivityState.IDLE: TickProfile(
        playhead_ms=66,
        labels_ms=250,
        network_ms=500,
        background_ms=1000,
    ),
    ActivityState.HIDDEN: TickProfile(
        playhead_ms=500,
        labels_ms=500,
        network_ms=1000,
        background_ms=2000,
    ),
}


class LoadGovernor:
    """Watches how much of the tick budget the UI work actually consumes.

    The scheduler asks for a multiplier before each dispatch. While ticks stay
    cheap the multiplier is 1.0 and nothing changes. When the measured cost of
    a full second of UI work crosses ``target_load`` the multiplier climbs,
    stretching every interval, and it relaxes again once the load drops. This
    is what keeps a busy machine responsive instead of letting Qt pile up
    timer events behind slow slots.
    """

    def __init__(
        self,
        *,
        target_load: float = 0.10,
        max_scale: float = 6.0,
        window_seconds: float = 1.0,
    ) -> None:
        self.target_load = max(0.01, float(target_load))
        self.max_scale = max(1.0, float(max_scale))
        self.window_seconds = max(0.25, float(window_seconds))
        self._window_started = time.perf_counter()
        self._busy_seconds = 0.0
        self._scale = 1.0
        self._last_load = 0.0

    @property
    def scale(self) -> float:
        return self._scale

    @property
    def load(self) -> float:
        """Fraction of wall-clock time the last window spent in UI work."""
        return self._last_load

    def record(self, elapsed_seconds: float) -> None:
        self._busy_seconds += max(0.0, float(elapsed_seconds))
        now = time.perf_counter()
        span = now - self._window_started
        if span < self.window_seconds:
            return

        load = self._busy_seconds / max(1e-6, span)
        self._last_load = load
        self._busy_seconds = 0.0
        self._window_started = now

        if load > self.target_load:
            # Stretch proportionally to how far over budget we are, but move
            # in steps so one slow frame cannot halve the refresh rate.
            self._scale = min(self.max_scale, self._scale * 1.35)
        elif load < self.target_load * 0.5:
            self._scale = max(1.0, self._scale * 0.85)

    def reset(self) -> None:
        self._scale = 1.0
        self._busy_seconds = 0.0
        self._last_load = 0.0
        self._window_started = time.perf_counter()


@dataclass
class _Job:
    tier: Tier
    callback: Callable[[], None]
    name: str
    next_due: float = 0.0
    calls: int = 0
    total_seconds: float = 0.0
    enabled: bool = True
    failures: int = 0

    @property
    def average_ms(self) -> float:
        if not self.calls:
            return 0.0
        return (self.total_seconds / self.calls) * 1000.0


class UiTickScheduler(QObject):
    """Single-timer, tier-paced dispatcher for periodic UI work."""

    state_changed = Signal(str)
    job_failed = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jobs: list[_Job] = []
        self._state = ActivityState.IDLE
        self._governor = LoadGovernor(
            target_load=_env_float("TRACYY_UI_TARGET_LOAD", 0.10),
        )
        self._profile = PROFILES[self._state]
        self._paused = False

        self._timer = QTimer(self)
        # CoarseTimer lets Qt align wakeups with other events instead of
        # holding a high-resolution timer open for the whole show. Only the
        # playhead tier would notice, and it tolerates a couple of ms.
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self._dispatch)
        self._apply_profile()

    # -- registration ----------------------------------------------------

    def add(
        self,
        tier: Tier,
        callback: Callable[[], None],
        *,
        name: str = "",
    ) -> None:
        self._jobs.append(
            _Job(
                tier=tier,
                callback=callback,
                name=name or getattr(callback, "__name__", "job"),
            )
        )

    def set_job_enabled(self, name: str, enabled: bool) -> None:
        for job in self._jobs:
            if job.name == name:
                job.enabled = bool(enabled)

    def is_job_enabled(self, name: str) -> bool:
        return any(job.name == name and job.enabled for job in self._jobs)

    def handle(self, name: str) -> JobHandle:
        """A QTimer-shaped handle for one job.

        Existing code turns its periodic work on and off with
        ``timer.start()`` / ``timer.stop()`` / ``timer.isActive()``. Folding
        those timers into the scheduler would otherwise mean touching every
        such call site, so the scheduler hands out something that answers to
        the same three names.
        """
        return JobHandle(self, name)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._paused = False
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def pause(self) -> None:
        """Suspend every tier — used while a modal dialog owns the screen."""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    # -- state -----------------------------------------------------------

    @property
    def state(self) -> ActivityState:
        return self._state

    def set_state(self, state: ActivityState) -> None:
        if state is self._state:
            return
        self._state = state
        self._governor.reset()
        self._apply_profile()
        self.state_changed.emit(state.value)

    def update_state(self, *, playing: bool, visible: bool) -> None:
        if not visible:
            self.set_state(ActivityState.HIDDEN)
        elif playing:
            self.set_state(ActivityState.PLAYING)
        else:
            self.set_state(ActivityState.IDLE)

    def _apply_profile(self) -> None:
        self._profile = PROFILES[self._state].scaled(self._governor.scale)
        # The timer runs at the fastest tier; slower tiers are decimated in
        # _dispatch. One timer means one wakeup source for the whole UI.
        self._timer.setInterval(max(8, self._profile.playhead_ms))
        now = time.perf_counter()
        for job in self._jobs:
            job.next_due = now

    # -- dispatch --------------------------------------------------------

    def _dispatch(self) -> None:
        if self._paused:
            return

        now = time.perf_counter()
        started = now
        previous_scale = self._governor.scale

        for job in self._jobs:
            if not job.enabled or now < job.next_due:
                continue
            job_started = time.perf_counter()
            try:
                job.callback()
            except Exception as exc:
                # A single broken periodic update must not take the show down
                # and must not stop the tiers that still work. Three strikes
                # and the job is retired for the rest of the session.
                job.failures += 1
                if job.failures >= 3:
                    job.enabled = False
                self.job_failed.emit(job.name, str(exc))
            elapsed = time.perf_counter() - job_started
            job.calls += 1
            job.total_seconds += elapsed
            interval = self._profile.interval_for(job.tier) / 1000.0
            # Absolute scheduling, but never let a long stall queue up a burst
            # of catch-up calls.
            job.next_due = max(now + interval * 0.5, job.next_due + interval)

        self._governor.record(time.perf_counter() - started)
        if abs(self._governor.scale - previous_scale) > 1e-6:
            self._apply_profile()

    # -- diagnostics -----------------------------------------------------

    def statistics(self) -> dict[str, object]:
        return {
            "state": self._state.value,
            "load": round(self._governor.load, 4),
            "scale": round(self._governor.scale, 3),
            "interval_ms": self._timer.interval(),
            "jobs": [
                {
                    "name": job.name,
                    "tier": job.tier.value,
                    "calls": job.calls,
                    "avg_ms": round(job.average_ms, 3),
                    "enabled": job.enabled,
                }
                for job in sorted(
                    self._jobs,
                    key=lambda item: item.total_seconds,
                    reverse=True,
                )
            ],
        }


class RateLimiter:
    """Allow an action at most once per interval. Not a timer, just a gate."""

    __slots__ = ("_interval", "_last")

    def __init__(self, interval_seconds: float) -> None:
        self._interval = max(0.0, float(interval_seconds))
        self._last = 0.0

    def ready(self, *, now: float | None = None) -> bool:
        moment = time.perf_counter() if now is None else float(now)
        if moment - self._last < self._interval:
            return False
        self._last = moment
        return True

    def reset(self) -> None:
        self._last = 0.0


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class JobHandle:
    """QTimer-compatible on/off switch for a scheduled job."""

    __slots__ = ("_name", "_scheduler")

    def __init__(self, scheduler: UiTickScheduler, name: str) -> None:
        self._scheduler = scheduler
        self._name = name

    def start(self, _interval_ms: int | None = None) -> None:
        self._scheduler.set_job_enabled(self._name, True)

    def stop(self) -> None:
        self._scheduler.set_job_enabled(self._name, False)

    def isActive(self) -> bool:
        return self._scheduler.is_job_enabled(self._name)
