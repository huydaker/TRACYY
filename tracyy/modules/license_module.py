from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar


class LicenseState(str, Enum):
    BETA = "BETA"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"
    REVOKED = "REVOKED"

    @classmethod
    def normalize(cls, value: object) -> LicenseState:
        text = str(value or "").strip().upper()
        try:
            return cls(text)
        except ValueError:
            return cls.BLOCKED


@dataclass(frozen=True)
class LicenseAccess:
    file: bool
    server: bool
    settings: bool
    web: bool
    cue_countdown: bool
    message: str
    style: str


class LicensePolicy:
    """Single source of truth for Tracyy feature permissions."""

    _MATRIX: ClassVar[dict[LicenseState, LicenseAccess]] = {
        LicenseState.BETA: LicenseAccess(
            file=True,
            server=True,
            settings=True,
            web=True,
            cue_countdown=True,
            message="Beta access — all features enabled; waiting for the latest update",
            style="active",
        ),
        LicenseState.ACTIVE: LicenseAccess(
            file=True,
            server=True,
            settings=True,
            web=True,
            cue_countdown=True,
            message="License active — all features enabled",
            style="active",
        ),
        LicenseState.EXPIRED: LicenseAccess(
            file=True,
            server=True,
            settings=True,
            web=True,
            cue_countdown=True,
            message="License expired — local playback remains available",
            style="expired",
        ),
        LicenseState.BLOCKED: LicenseAccess(
            file=True,
            server=False,
            settings=True,
            web=True,
            cue_countdown=True,
            message="License blocked — Server is disabled",
            style="blocked",
        ),
        LicenseState.REVOKED: LicenseAccess(
            file=True,
            server=False,
            settings=True,
            web=True,
            cue_countdown=False,
            message="License revoked — Server and cue countdown are disabled",
            style="blocked",
        ),
    }

    def __init__(self, status: object) -> None:
        self.state = LicenseState.normalize(status)
        self.access = self._MATRIX[self.state]

    @property
    def status(self) -> str:
        return self.state.value

    def allows(self, feature: str) -> bool:
        return bool(getattr(self.access, str(feature).strip().lower(), False))
