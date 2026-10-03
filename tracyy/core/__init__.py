"""Shared building blocks.

Some of these need Qt and some deliberately do not: :mod:`tracyy.core.paths`
and :mod:`tracyy.core.logs` are imported by spawned decoder and analysis
workers, and by the standalone cue-viewer server, none of which should pay for
the widget stack.

Importing a submodule normally runs its package ``__init__`` first, so a plain
re-export here would drag PySide6 into every one of those processes. The Qt
names are therefore resolved lazily: ``from tracyy.core import paths`` costs
nothing, while ``from tracyy.core import AdaptiveWrapLabel`` imports Qt at the
moment it is actually asked for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .logs import configure as configure_logging
from .logs import get_logger
from .paths import (
    app_root,
    cache_root,
    data_root,
    icon_dir,
    is_frozen,
    resource_root,
    source_root,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .perf import ActivityState, RateLimiter, Tier, UiTickScheduler
    from .reactive import (
        ValueCache,
        set_checked,
        set_enabled,
        set_line_text,
        set_property,
        set_style_sheet,
        set_text,
        set_tooltip,
        set_value,
        set_visible,
    )
    from .ui_helpers import (
        AdaptiveWrapLabel,
        local_lan_address,
        seconds_to_clock,
        silent_message_box,
        tracyy_app_root,
        tracyy_data_root,
        tracyy_resource_root,
    )

#: Names that live in Qt-dependent submodules, resolved on first access.
_LAZY = {
    "ActivityState": "perf",
    "RateLimiter": "perf",
    "Tier": "perf",
    "UiTickScheduler": "perf",
    "AdaptiveWrapLabel": "ui_helpers",
    "local_lan_address": "ui_helpers",
    "seconds_to_clock": "ui_helpers",
    "silent_message_box": "ui_helpers",
    "tracyy_app_root": "ui_helpers",
    "tracyy_data_root": "ui_helpers",
    "tracyy_resource_root": "ui_helpers",
    "ValueCache": "reactive",
    "set_checked": "reactive",
    "set_enabled": "reactive",
    "set_line_text": "reactive",
    "set_property": "reactive",
    "set_style_sheet": "reactive",
    "set_text": "reactive",
    "set_tooltip": "reactive",
    "set_value": "reactive",
    "set_visible": "reactive",
}

__all__ = [
    "ActivityState",
    "AdaptiveWrapLabel",
    "RateLimiter",
    "Tier",
    "UiTickScheduler",
    "ValueCache",
    "app_root",
    "cache_root",
    "configure_logging",
    "data_root",
    "get_logger",
    "icon_dir",
    "is_frozen",
    "local_lan_address",
    "resource_root",
    "seconds_to_clock",
    "set_checked",
    "set_enabled",
    "set_line_text",
    "set_property",
    "set_style_sheet",
    "set_text",
    "set_tooltip",
    "set_value",
    "set_visible",
    "silent_message_box",
    "source_root",
    "tracyy_app_root",
    "tracyy_data_root",
    "tracyy_resource_root",
]


def __getattr__(name: str):
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    return getattr(importlib.import_module(f".{module_name}", __name__), name)


def __dir__() -> list[str]:
    return sorted(__all__)
