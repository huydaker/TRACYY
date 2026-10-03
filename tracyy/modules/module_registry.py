from __future__ import annotations

from .base import FeatureAccess
from .file_module import FileModule
from .server_module import ServerModule
from .settings_module import SettingsModule
from .web_module import WebModule


class TracyyModuleRegistry:
    """Owns page-level feature modules and applies license gates consistently."""

    def __init__(self) -> None:
        self.file = FileModule()
        self.server = ServerModule()
        self.settings = SettingsModule()
        self.web = WebModule()

    def apply_license(self, window: object, policy: object) -> None:
        access = policy.access
        self.file.apply_access(window, FeatureAccess(enabled=access.file))
        self.server.apply_access(window, FeatureAccess(enabled=access.server))
        self.settings.apply_access(window, FeatureAccess(enabled=access.settings))
        self.web.apply_access(window, FeatureAccess(enabled=access.web))

        pages = getattr(window, "pages", None)
        current_index = pages.currentIndex() if pages is not None else 0
        if (current_index == 1 and not access.settings) or (
            current_index == 2 and not access.server
        ):
            window.switch_page(0)
