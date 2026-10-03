from __future__ import annotations

from PySide6.QtCore import Qt

AUDIO_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".oga",
    ".aiff",
    ".aif",
    ".wma",
    ".opus",
    ".caf",
}

MARKER_SHAPE_OPTIONS = [
    ("Rectangle", "rectangle"),
    ("Triangle", "triangle"),
    ("Diamond", "diamond"),
    ("Circle", "circle"),
    ("Heart", "heart"),
]
MARKER_SHAPE_VALUES = {value for _label, value in MARKER_SHAPE_OPTIONS}


def normalize_marker_shape(value: object) -> str:
    shape = str(value or "").strip().lower()
    if shape not in MARKER_SHAPE_VALUES:
        return "rectangle"
    return shape


PLAYLIST_TITLE_ROLE = int(Qt.ItemDataRole.UserRole) + 10
PLAYLIST_PROGRESS_ROLE = int(Qt.ItemDataRole.UserRole) + 11
PLAYLIST_MISSING_ROLE = int(Qt.ItemDataRole.UserRole) + 12
PLAYLIST_MEDIA_META_ROLE = int(Qt.ItemDataRole.UserRole) + 13
