"""One entry point for everything licence-related.

V1 spread this over five modules and made the UI reach through all of them:
the main window held a ``LicenseClient``, poked ``client.usb_admin`` for USB
state, called :func:`resolve_license_countdown` with hand-assembled arguments,
wrote ``type(client)._startup_result`` directly to seed the process cache, and
ran its own threads for the two scan buttons. The same three or four lines of
"work out the current policy and re-apply it" appeared in half a dozen places.

:class:`LicenseService` owns that state instead. The wire protocol, the cache
format, the USB token verification and the countdown rules are unchanged — the
same :mod:`~tracyy.licensing.client`, :mod:`~tracyy.licensing.usb_admin` and
:mod:`~tracyy.licensing.countdown` code does the work — but callers now see one
object with one vocabulary, and background scans are started here rather than
being open-coded in the window.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from tracyy.core.logs import get_logger
from tracyy.modules.license_module import LicensePolicy

from .client import OFFLINE_GRACE_HOURS, LicenseClient, LicenseResult
from .countdown import LicenseCountdownSnapshot, resolve_license_countdown
from .usb_admin import USBRuntimeState

__all__ = ["LicenseService", "LicenseSnapshot"]

_log = get_logger(__name__)


class LicenseSnapshot:
    """Everything the UI needs to render licence state, resolved together."""

    __slots__ = ("countdown", "policy", "result", "usb_state")

    def __init__(
        self,
        result: LicenseResult,
        policy: LicensePolicy,
        countdown: LicenseCountdownSnapshot,
        usb_state: USBRuntimeState,
    ) -> None:
        self.result = result
        self.policy = policy
        self.countdown = countdown
        self.usb_state = usb_state

    @property
    def status(self) -> str:
        return str(self.result.status or "").strip().upper()

    @property
    def active(self) -> bool:
        return bool(self.result.active)


class LicenseService:
    """Owns the licence client, the USB manager and the derived state."""

    def __init__(self, *, offline_grace_hours: float = OFFLINE_GRACE_HOURS) -> None:
        self.offline_grace_hours = float(offline_grace_hours)
        self._client = LicenseClient()
        self._result: LicenseResult | None = None
        self._scan_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None

    # -- accessors -------------------------------------------------------

    @property
    def client(self) -> LicenseClient:
        """The underlying client. Prefer the service's own API where it exists."""
        return self._client

    @property
    def result(self) -> LicenseResult | None:
        return self._result

    @property
    def usb_state(self) -> USBRuntimeState:
        return self._client.usb_runtime_state

    @property
    def usb_warning_seconds(self) -> float:
        return float(getattr(self._client.usb_admin, "warning_seconds", 0.0) or 0.0)

    @property
    def scan_in_progress(self) -> bool:
        thread = self._scan_thread
        return bool(thread is not None and thread.is_alive())

    def config_debug_info(self) -> dict:
        return self._client.config_debug_info()

    # -- checks ----------------------------------------------------------

    def check(self, *, force_refresh: bool = False) -> LicenseResult:
        """Resolve the licence, contacting the server when allowed.

        Blocking; call it from a worker thread or during startup, never from a
        periodic UI job.
        """
        result = self._client.check(force_refresh=force_refresh)
        self._result = result
        return result

    def remember(self, result: LicenseResult) -> None:
        """Adopt a result and make it the process-wide startup answer.

        V1 assigned ``type(client)._startup_result`` from the window; the
        knowledge that the cache is a class attribute stays in here now.
        """
        self._result = result
        LicenseClient._startup_result = result

    def clear_process_cache(self) -> None:
        LicenseClient.clear_process_cache()

    def server_reachable(self, *, timeout: float = 2.0) -> bool:
        return bool(self._client.server_reachable(timeout=timeout))

    # -- derived state ---------------------------------------------------

    def countdown(
        self,
        *,
        now: float | None = None,
        result: LicenseResult | None = None,
    ) -> LicenseCountdownSnapshot:
        return resolve_license_countdown(
            result if result is not None else self._result,
            now=time.time() if now is None else float(now),
            default_grace_hours=self.offline_grace_hours,
            usb_runtime_state=self.usb_state,
        )

    def policy(self, *, result: LicenseResult | None = None) -> LicensePolicy:
        target = result if result is not None else self._result
        return LicensePolicy(getattr(target, "status", "BLOCKED"))

    def snapshot(self, *, now: float | None = None) -> LicenseSnapshot | None:
        if self._result is None:
            return None
        return LicenseSnapshot(
            self._result,
            self.policy(),
            self.countdown(now=now),
            self.usb_state,
        )

    # -- USB -------------------------------------------------------------

    def poll_usb(self, now: float | None = None) -> USBRuntimeState:
        state = self._client.usb_admin.poll(now)
        self._client.usb_runtime_state = state
        return state

    def prepare_shutdown(self) -> None:
        try:
            self._client.usb_admin.prepare_shutdown(self.usb_state)
        except Exception:
            _log.warning("USB shutdown state could not be cleared", exc_info=True)

    # -- background scans ------------------------------------------------

    def scan_usb_async(
        self,
        on_finished: Callable[[USBRuntimeState, str], None],
    ) -> bool:
        """Rescan mounted volumes for an admin token, off the UI thread.

        ``on_finished`` is called from the worker thread with the new state and
        an error string (empty on success). Returns False when a scan is
        already running.
        """

        def work() -> tuple[USBRuntimeState, str]:
            state = self._client.usb_admin.force_rescan()
            self._client.usb_runtime_state = state
            return state, ""

        def failed(exc: Exception) -> tuple[USBRuntimeState, str]:
            return self.usb_state, str(exc)

        return self._start_scan("usb", work, failed, on_finished)

    def scan_google_sheet_async(
        self,
        on_finished: Callable[[LicenseResult | None, str], None],
    ) -> bool:
        """Re-query the licence sheet off the UI thread."""

        def work() -> tuple[LicenseResult, str]:
            result = self._client.scan_google_sheet()
            return result, ""

        def failed(exc: Exception) -> tuple[None, str]:
            return None, str(exc)

        return self._start_scan("sheet", work, failed, on_finished)

    def _start_scan(
        self,
        name: str,
        work: Callable[[], tuple],
        failed: Callable[[Exception], tuple],
        on_finished: Callable[..., None],
    ) -> bool:
        with self._scan_lock:
            if self.scan_in_progress:
                return False

            def run() -> None:
                try:
                    payload = work()
                except Exception as exc:
                    _log.warning("%s licence scan failed: %s", name, exc)
                    payload = failed(exc)
                try:
                    on_finished(*payload)
                except Exception:
                    _log.exception("licence scan callback raised")

            thread = threading.Thread(
                target=run,
                name=f"TracyyLicenseScan-{name}",
                daemon=True,
            )
            self._scan_thread = thread
            thread.start()
            return True
