"""Qt user interface."""

from __future__ import annotations

__all__ = ["MainWindow", "TracyyApplication"]


def __getattr__(name: str):
    if name in __all__:
        from . import main_window

        return getattr(main_window, name)
    raise AttributeError(name)
