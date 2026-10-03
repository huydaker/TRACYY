from __future__ import annotations

from .base import FeatureModule


class ServerModule(FeatureModule):
    name = "server"

    def widgets(self, window: object):
        widgets = []
        widget = getattr(window, "nav_server", None)
        if widget is not None:
            widgets.append(widget)
        return tuple(widgets)
