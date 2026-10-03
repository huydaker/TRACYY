from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureAccess:
    enabled: bool = True
    visible: bool = True


class FeatureModule:
    """Small UI feature boundary used while main.py is split incrementally."""

    name = "feature"

    def widgets(self, window: object) -> Iterable[object]:
        return ()

    def apply_access(self, window: object, access: FeatureAccess) -> None:
        for widget in self.widgets(window):
            if widget is None:
                continue
            set_enabled = getattr(widget, "setEnabled", None)
            if callable(set_enabled):
                set_enabled(bool(access.enabled))
            set_visible = getattr(widget, "setVisible", None)
            if callable(set_visible):
                set_visible(bool(access.visible))
