"""Tracyy V2 — LTC-synchronised show playback.

This module is imported by spawned decoder/analysis worker processes, so it
must stay free of Qt, sounddevice and every other heavy import. Nothing but
version metadata belongs here.
"""

from __future__ import annotations

__all__ = ["APP_DISPLAY_VERSION", "APP_NAME", "__version__"]

__version__ = "2.1.0"
APP_NAME = "Tracyy"
APP_DISPLAY_VERSION = f"V{__version__}"
