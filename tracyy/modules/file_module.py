from __future__ import annotations

from .base import FeatureModule


class FileModule(FeatureModule):
    name = "file"

    def widgets(self, window: object):
        widgets = []
        widget = getattr(window, "nav_file", None)
        if widget is not None:
            widgets.append(widget)
        return tuple(widgets)
