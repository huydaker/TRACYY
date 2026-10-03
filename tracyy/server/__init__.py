"""LAN cue viewer server."""

from __future__ import annotations

__all__ = ["run_cue_viewer_server"]


def __getattr__(name: str):
    if name == "run_cue_viewer_server":
        from .cue_viewer import main as run_cue_viewer_server

        return run_cue_viewer_server
    raise AttributeError(name)
