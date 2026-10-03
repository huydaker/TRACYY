"""Transport glyphs drawn at runtime instead of shipped as bitmaps.

The five transport buttons used to load five unrelated clipart PNGs: a purple
flat-design tile with a drop shadow for back-to-start, grey discs for skip, a
white rounded square for stop, a green ring for play. One bar, five visual
languages, and none of them tintable.

Drawing them here gives one optical weight, one colour, and one size for the
whole row, lets the colour follow state (the play glyph goes green while the
transport runs), and keeps them sharp on a HiDPI screen because each pixmap is
rendered at the device pixel ratio rather than scaled up from a fixed bitmap.

Every glyph is composed inside a 20x20 unit box and scaled to the requested
size, so proportions stay identical no matter what the caller asks for.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

__all__ = ["TRANSPORT_GLYPHS", "transport_icon"]

#: Glyph names this module can draw.
TRANSPORT_GLYPHS = (
    "previous",
    "next",
    "play",
    "pause",
    "stop",
    "zero",
)

#: Every path below is authored in this coordinate space and then scaled.
_UNIT = 20.0


def _triangle_right(path: QPainterPath, left: float, width: float) -> None:
    """A play-style triangle, vertically centred, pointing right."""
    height = 13.0
    top = (_UNIT - height) / 2.0
    path.moveTo(left, top)
    path.lineTo(left, top + height)
    path.lineTo(left + width, _UNIT / 2.0)
    path.closeSubpath()


def _triangle_left(path: QPainterPath, right: float, width: float) -> None:
    """Mirror of :func:`_triangle_right`."""
    height = 13.0
    top = (_UNIT - height) / 2.0
    path.moveTo(right, top)
    path.lineTo(right, top + height)
    path.lineTo(right - width, _UNIT / 2.0)
    path.closeSubpath()


def _bar(path: QPainterPath, left: float, width: float) -> None:
    height = 13.0
    top = (_UNIT - height) / 2.0
    path.addRect(QRectF(left, top, width, height))


def _build_path(name: str) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)

    if name == "play":
        # Optically centred rather than geometrically: a right-pointing
        # triangle reads as left-heavy when its bounding box is centred.
        _triangle_right(path, 6.0, 10.0)

    elif name == "pause":
        _bar(path, 5.5, 3.2)
        _bar(path, 11.3, 3.2)

    elif name == "stop":
        side = 11.5
        offset = (_UNIT - side) / 2.0
        path.addRoundedRect(QRectF(offset, offset, side, side), 1.6, 1.6)

    elif name == "next":
        _triangle_right(path, 3.0, 8.0)
        _triangle_right(path, 9.4, 8.0)
        _bar(path, 16.0, 2.4)

    elif name == "previous":
        _triangle_left(path, 17.0, 8.0)
        _triangle_left(path, 10.6, 8.0)
        _bar(path, 1.6, 2.4)

    elif name == "zero":
        # Back to start: bar plus a single triangle. Distinct from the double
        # triangle of "previous track", which is a different action.
        _bar(path, 3.4, 2.4)
        _triangle_left(path, 16.6, 9.0)

    else:
        raise ValueError(f"unknown transport glyph: {name!r}")

    return path


def transport_icon(
    name: str,
    color: str,
    size: int = 20,
    ratio: float = 2.0,
) -> QIcon:
    """One transport glyph, filled in ``color``, as a ``size`` px icon."""
    scale = max(1.0, float(ratio))
    pixels = max(1, round(size * scale))

    pixmap = QPixmap(pixels, pixels)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Logical units, not device pixels. A pixmap carrying a device pixel
        # ratio already makes QPainter scale by that ratio on its own, so
        # scaling by the device size here would apply the ratio twice and push
        # three quarters of the glyph outside the pixmap.
        painter.scale(size / _UNIT, size / _UNIT)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QColor(color))
        painter.drawPath(_build_path(name))
    finally:
        painter.end()

    return QIcon(pixmap)
