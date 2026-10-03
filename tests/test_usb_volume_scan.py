"""A volume that stops answering must not wedge the licence scan.

A stale network mount — the file server is gone, the mount point is not —
blocks in ``is_dir()`` and ``iterdir()`` instead of raising. The Admin USB
scan walks every mounted volume, runs on the GUI thread, and runs before the
main window is built, so one dead share used to mean Tracyy never appeared.

The volumes here answer through an ``Event`` that is never set, which is what
a stale mount does: no error, no timeout, no answer.
"""

from __future__ import annotations

import os
import platform
import threading
import time

import pytest

from tracyy.licensing import usb_admin, volume_scan
from tracyy.licensing.usb_admin import (
    TOKEN_DIRECTORY,
    TOKEN_FILENAME,
    AdminUSBManager,
    USBRuntimeState,
    _ScanBudget,
)

#: Short enough to keep the suite quick, long enough that a working volume
#: answers well inside it.
PROBE_SECONDS = 0.25


@pytest.fixture(autouse=True)
def isolated_probes(monkeypatch):
    """Probe results are process-global, so no test may inherit another's."""
    volume_scan.reset()
    monkeypatch.setattr(volume_scan, "PROBE_SECONDS", PROBE_SECONDS)
    monkeypatch.setattr(volume_scan, "SCAN_BUDGET_SECONDS", PROBE_SECONDS * 4)
    yield
    volume_scan.reset()


@pytest.fixture
def gates():
    """Hands out block/release gates and releases them all at teardown.

    Without the release a wedged worker would outlive the test, exactly as a
    parked probe outlives a stale mount in the running application.
    """
    created: list[threading.Event] = []

    def make() -> threading.Event:
        gate = threading.Event()
        created.append(gate)
        return gate

    yield make

    for gate in created:
        gate.set()


class FakeVolume:
    """One mounted volume, with control over how it answers.

    ``gate`` is waited on before every call, so a gate that is never set is a
    stale mount. ``error`` is the other failure the scan has to survive: a
    volume that raises rather than hangs.
    """

    def __init__(
        self,
        name: str,
        *,
        entries=(),
        gate: threading.Event | None = None,
        error: Exception | None = None,
        is_dir: bool = True,
        is_file: bool = False,
    ) -> None:
        self.name = name
        self.entries = list(entries)
        self.gate = gate
        self.error = error
        self._is_dir = is_dir
        self._is_file = is_file
        self.touches = 0

    def _answer(self) -> None:
        self.touches += 1
        if self.gate is not None:
            self.gate.wait()
        if self.error is not None:
            raise self.error

    def __fspath__(self) -> str:
        return f"/Volumes/{self.name}"

    def __str__(self) -> str:
        return self.__fspath__()

    def __repr__(self) -> str:
        return f"FakeVolume({self.name!r})"

    def __lt__(self, other: FakeVolume) -> bool:
        # The scan sorts the entries it lists, as Path objects allow.
        return self.name < other.name

    def __truediv__(self, other: str) -> FakeVolume:
        for entry in self.entries:
            if entry.name == other:
                return entry
        return FakeVolume(
            f"{self.name}/{other}",
            gate=self.gate,
            error=self.error,
            is_dir=False,
        )

    def iterdir(self) -> list[FakeVolume]:
        self._answer()
        return list(self.entries)

    def is_dir(self) -> bool:
        self._answer()
        return self._is_dir

    def is_file(self) -> bool:
        self._answer()
        return self._is_file


def use_roots(monkeypatch, roots) -> None:
    """Mount ``roots`` as the volumes the scan will walk."""
    monkeypatch.setattr(usb_admin.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        AdminUSBManager,
        "_mounted_volume_roots",
        classmethod(lambda cls, base, budget, depth=1: list(roots)),
    )


def install_token(root, body: str = "not a real token") -> None:
    (root / TOKEN_DIRECTORY).mkdir(parents=True, exist_ok=True)
    (root / TOKEN_DIRECTORY / TOKEN_FILENAME).write_text(body, encoding="utf-8")


# -- the deadline probe --------------------------------------------------


def test_a_blocked_probe_gives_up_and_reports_pending(gates):
    gate = gates()
    started = time.monotonic()
    outcome = volume_scan.run_with_deadline("stale", gate.wait, timeout=PROBE_SECONDS)
    elapsed = time.monotonic() - started

    assert outcome is volume_scan.PENDING
    assert elapsed < PROBE_SECONDS * 4, "the deadline did not bound the wait"


def test_a_blocked_probe_costs_one_worker_not_one_per_call(gates):
    gate = gates()
    calls: list[int] = []

    def blocked() -> str:
        calls.append(1)
        gate.wait()
        return "answered"

    assert volume_scan.run_with_deadline("stale", blocked, PROBE_SECONDS) is volume_scan.PENDING

    started = time.monotonic()
    for _ in range(5):
        assert (
            volume_scan.run_with_deadline("stale", blocked, PROBE_SECONDS)
            is volume_scan.PENDING
        )
    elapsed = time.monotonic() - started

    # The poll runs once a second for the life of the process. Re-waiting the
    # deadline, or starting a second worker, would be paid every time.
    assert len(calls) == 1, "a second worker was started on a mount already known to be stuck"
    assert elapsed < PROBE_SECONDS, "later calls waited on the stuck volume again"


def test_a_slow_volume_is_collected_once_it_answers(gates):
    gate = gates()

    def slow() -> str:
        gate.wait()
        return "the dongle"

    assert volume_scan.run_with_deadline("slow", slow, PROBE_SECONDS) is volume_scan.PENDING

    gate.set()
    for _ in range(50):
        outcome = volume_scan.run_with_deadline("slow", slow, PROBE_SECONDS)
        if outcome is not volume_scan.PENDING:
            break
        time.sleep(0.02)

    # A timeout means "not found yet", so the answer has to survive to a
    # later scan rather than being thrown away as "no dongle".
    assert outcome == "the dongle"


def test_a_failing_probe_reports_its_error():
    def boom() -> None:
        raise OSError("the volume went away")

    with pytest.raises(OSError, match="went away"):
        volume_scan.run_with_deadline("broken", boom, PROBE_SECONDS)


def test_forget_completed_keeps_tracking_workers_still_in_flight(gates):
    gate = gates()
    calls: list[int] = []

    def blocked() -> None:
        calls.append(1)
        gate.wait()

    volume_scan.run_with_deadline("stale", blocked, PROBE_SECONDS)
    volume_scan.forget_completed()
    volume_scan.run_with_deadline("stale", blocked, PROBE_SECONDS)

    assert len(calls) == 1, "a manual rescan started a second worker on a stuck mount"


# -- network volumes -----------------------------------------------------


def test_the_nearest_mount_point_decides_the_filesystem(monkeypatch):
    monkeypatch.setattr(
        volume_scan,
        "mount_table",
        lambda: (("/", "apfs"), ("/Volumes/Share", "smbfs"), ("/Volumes/Share/usb", "exfat")),
    )

    assert volume_scan.is_network_path("/Volumes/Share") is True
    assert volume_scan.is_network_path("/Volumes/Share/cues") is True
    # A local volume mounted inside a share is still local.
    assert volume_scan.is_network_path("/Volumes/Share/usb") is False
    assert volume_scan.is_network_path("/Volumes/Dongle") is False


def test_an_unreadable_mount_table_costs_the_shortcut_not_a_dongle(monkeypatch):
    monkeypatch.setattr(volume_scan, "mount_table", tuple)

    # Unknown has to mean "scan it", never "skip it".
    assert volume_scan.is_network_path("/Volumes/Anything") is False


@pytest.mark.skipif(platform.system() != "Darwin", reason="reads the macOS mount table")
def test_the_real_mount_table_names_this_machines_volumes():
    table = dict(volume_scan.mount_table())

    assert table.get("/") == "apfs"
    for point, fstype in table.items():
        if point.startswith("/Volumes/"):
            assert volume_scan.is_network_path(point) == (
                fstype in volume_scan.NETWORK_FILESYSTEMS
            )


def test_a_network_volume_is_skipped_without_being_touched(monkeypatch, gates):
    share = FakeVolume("Share", gate=gates())
    dongle = FakeVolume("Dongle")
    base = FakeVolume("base", entries=[share, dongle])
    monkeypatch.setattr(
        volume_scan,
        "is_network_path",
        lambda path: os.fspath(path).endswith("/Share"),
    )

    budget = _ScanBudget()
    roots = AdminUSBManager._mounted_volume_roots(base, budget)

    assert roots == [dongle]
    # Deciding from the mount table means the stale share is never called at
    # all — nothing to time out, and no parked worker.
    assert share.touches == 0
    assert budget.incomplete is False


# -- walking the volumes -------------------------------------------------


def test_a_volume_that_blocks_is_skipped_and_the_others_are_kept(monkeypatch, gates):
    stale = FakeVolume("LIVE", gate=gates())
    dongle = FakeVolume("Dongle")
    base = FakeVolume("base", entries=[stale, dongle])
    monkeypatch.setattr(volume_scan, "is_network_path", lambda path: False)

    budget = _ScanBudget()
    started = time.monotonic()
    roots = AdminUSBManager._mounted_volume_roots(base, budget)
    elapsed = time.monotonic() - started

    assert roots == [dongle]
    assert budget.incomplete is True, "a skipped volume must be reported as unscanned"
    assert elapsed < PROBE_SECONDS * 6


def test_a_volume_that_raises_is_skipped(monkeypatch):
    broken = FakeVolume("Broken", error=OSError("input/output error"))
    dongle = FakeVolume("Dongle")
    base = FakeVolume("base", entries=[broken, dongle])
    monkeypatch.setattr(volume_scan, "is_network_path", lambda path: False)

    budget = _ScanBudget()
    roots = AdminUSBManager._mounted_volume_roots(base, budget)

    assert roots == [dongle]
    # An error is an answer: the volume was scanned and has nothing.
    assert budget.incomplete is False


def test_a_dongle_is_still_found_behind_a_stale_mount(monkeypatch, tmp_path, gates):
    stale = FakeVolume("LIVE", gate=gates())
    working = tmp_path / "Dongle"
    working.mkdir()
    install_token(working)
    use_roots(monkeypatch, [stale, working])

    budget = _ScanBudget()
    started = time.monotonic()
    paths = AdminUSBManager._candidate_token_paths(budget)
    elapsed = time.monotonic() - started

    assert paths == [working / TOKEN_DIRECTORY / TOKEN_FILENAME]
    assert budget.incomplete is True
    assert elapsed < volume_scan.SCAN_BUDGET_SECONDS * 3


def test_the_scan_stays_bounded_however_many_volumes_hang(monkeypatch, gates):
    gate = gates()
    stale = [FakeVolume(f"Dead{index}", gate=gate) for index in range(12)]
    use_roots(monkeypatch, stale)

    started = time.monotonic()
    paths = AdminUSBManager._candidate_token_paths(_ScanBudget())
    elapsed = time.monotonic() - started

    assert paths == []
    # Per-volume deadlines alone would let the number of mounts set the delay.
    assert elapsed < volume_scan.SCAN_BUDGET_SECONDS * 3, f"took {elapsed:.2f}s"


# -- what the licence layer makes of it ----------------------------------


def test_startup_does_not_wait_on_a_stale_mount(monkeypatch, tmp_path, gates):
    """This is the hang: MainWindow.__init__ blocked here, before any window."""
    use_roots(monkeypatch, [FakeVolume("LIVE", gate=gates())])
    monkeypatch.setattr(usb_admin, "state_path", lambda: tmp_path / "usb_admin_state.dat")
    manager = AdminUSBManager("machine")

    started = time.monotonic()
    state = manager.startup_state()
    elapsed = time.monotonic() - started

    assert elapsed < volume_scan.SCAN_BUDGET_SECONDS * 3, f"startup blocked for {elapsed:.2f}s"
    assert state.mode == "NONE"
    assert state.active is False
    assert manager.last_scan_incomplete is True
    # An unfinished scan has to say so. "No licence" is a different claim.
    assert "chưa" in manager.last_detection_error


def test_an_unfinished_scan_keeps_the_verified_token(monkeypatch, gates):
    stale = FakeVolume("LIVE", gate=gates())
    use_roots(monkeypatch, [stale])
    manager = AdminUSBManager("machine")
    manager._cached_token_path = stale / TOKEN_FILENAME
    manager._cached_token_root = stale
    manager._cached_token_payload = {"token_id": "T-1"}
    manager._cached_token_signature = ("signature",)
    manager._cached_token_verified_at = 0.0

    assert manager.detect_valid_token() is None
    assert manager.last_scan_incomplete is True
    # Clearing here would make the dongle unrecoverable without a rescan,
    # even though nothing established that it had gone.
    assert manager._cached_token_path is not None


def test_an_unfinished_scan_is_not_reported_as_a_removed_dongle(monkeypatch, gates):
    use_roots(monkeypatch, [FakeVolume("LIVE", gate=gates())])
    manager = AdminUSBManager("machine")
    manager._session_authorized = True
    manager._last_token_id = "T-1"
    connected = USBRuntimeState(
        mode="USB_CONNECTED",
        connected=True,
        active=True,
        token_id="T-1",
    )
    manager._last_runtime_state = connected

    assert manager.poll() is connected


def test_a_finished_scan_still_reports_a_removed_dongle(monkeypatch):
    use_roots(monkeypatch, [])
    manager = AdminUSBManager("machine")
    manager._session_authorized = True
    manager._last_token_id = "T-1"
    manager._last_runtime_state = USBRuntimeState(
        mode="USB_CONNECTED",
        connected=True,
        active=True,
        token_id="T-1",
    )

    state = manager.poll()

    assert manager.last_scan_incomplete is False
    assert state.mode == "USB_REMOVED_SESSION"
    assert state.active is True
