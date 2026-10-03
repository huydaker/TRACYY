"""Licensing: server check, encrypted offline cache, admin-USB dongle.

:class:`~tracyy.licensing.service.LicenseService` is the entry point. The other
modules are the machinery behind it and are re-exported only for the tools in
``tools/`` and for callers that still need the raw types.
"""

from __future__ import annotations

from .client import OFFLINE_GRACE_HOURS, LicenseClient, LicenseResult
from .countdown import LicenseCountdownSnapshot, resolve_license_countdown
from .service import LicenseService, LicenseSnapshot
from .usb_admin import AdminUSBManager, USBRuntimeState

__all__ = [
    "OFFLINE_GRACE_HOURS",
    "AdminUSBManager",
    "LicenseClient",
    "LicenseCountdownSnapshot",
    "LicenseResult",
    "LicenseService",
    "LicenseSnapshot",
    "USBRuntimeState",
    "resolve_license_countdown",
]
