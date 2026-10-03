from __future__ import annotations

from .base import FeatureModule


class SettingsModule(FeatureModule):
    name = "settings"

    def widgets(self, window: object):
        widgets = []
        widget = getattr(window, "nav_settings", None)
        if widget is not None:
            widgets.append(widget)
        return tuple(widgets)
