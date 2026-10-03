from __future__ import annotations

from .base import FeatureModule


class WebModule(FeatureModule):
    name = "web"

    def widgets(self, window: object):
        widgets = []
        return tuple(widgets)
