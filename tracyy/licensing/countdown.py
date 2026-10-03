from __future__ import annotations

from dataclasses import dataclass
from typing import Any

OFFLINE_COUNTDOWN_STATES = {"ACTIVE", "BETA", "EXPIRED"}


@dataclass(frozen=True)
class LicenseCountdownSnapshot:
    mode: str
    deadline: float | None
    remaining_seconds: float
    applicable: bool

    @property
    def expired(self) -> bool:
        return self.mode == "expired"


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0.0 else None


def resolve_license_countdown(
    result: Any,
    *,
    now: float,
    default_grace_hours: float,
    usb_runtime_state: Any = None,
) -> LicenseCountdownSnapshot:
    status = str(getattr(result, "status", "") or "").strip().upper()
    source = str(getattr(result, "source", "") or "").strip().upper()
    online = bool(getattr(result, "online", False))
    usb_connected = bool(getattr(result, "usb_connected", False))

    if usb_connected or source == "ADMIN_USB_CONNECTED":
        return LicenseCountdownSnapshot("paused_usb", None, 0.0, False)
    if source == "ADMIN_USB_SESSION_REMOVED":
        return LicenseCountdownSnapshot("usb_session", None, 0.0, False)
    if online:
        return LicenseCountdownSnapshot("online", None, 0.0, False)
    if status not in OFFLINE_COUNTDOWN_STATES:
        return LicenseCountdownSnapshot("inactive", None, 0.0, False)

    deadline = _positive_float(getattr(result, "offline_valid_until", None)) or _positive_float(
        getattr(result, "expires_at", None)
    )
    if deadline is None:
        last_online = _positive_float(getattr(result, "last_online_check", None))
        if last_online is not None:
            deadline = last_online + max(0.0, float(default_grace_hours)) * 3600.0
    if deadline is None:
        return LicenseCountdownSnapshot("unavailable", None, 0.0, False)

    remaining = max(0.0, deadline - float(now))
    if remaining <= 0.0:
        return LicenseCountdownSnapshot("expired", deadline, 0.0, True)
    return LicenseCountdownSnapshot("countdown", deadline, remaining, True)
