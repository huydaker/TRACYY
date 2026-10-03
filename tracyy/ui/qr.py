"""QR codes for the cue-viewer address.

A crew member with a phone should not have to type an IP and a port. The code
is generated from the same string the card shows, so the two can never drift.

Two rendering rules decide whether a camera can read this at all, and both are
easy to get wrong:

*Modules land on whole pixels.* The module size is floored to an integer and
the matrix is drawn at that size, then centred in the requested box. Painting a
fractional module and letting Qt smooth it turns every edge into a gradient,
and a scanner thresholding that gradient reads the wrong bit.

*The quiet zone is part of the code.* The four-module light border is not
padding — the spec requires it, and a QR drawn flush to a dark panel edge often
will not decode. ``segno`` emits it by default and this keeps it.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap

__all__ = ["QR_AVAILABLE", "qr_pixmap"]

try:  # pragma: no cover - exercised by whether the dependency is installed
    import segno

    QR_AVAILABLE = True
except Exception:  # pragma: no cover
    segno = None
    QR_AVAILABLE = False


_CACHE: dict[tuple, QPixmap] = {}

#: A show has a handful of servers; more than this means something is looping.
_CACHE_LIMIT = 32


def qr_pixmap(
    data: str,
    size: int = 108,
    *,
    dark: str = "#0B1108",
    light: str = "#F2F5EF",
    ratio: float = 2.0,
) -> QPixmap | None:
    """A scannable QR for ``data``, or ``None`` when segno is unavailable."""
    text = str(data or "").strip()
    if not text or not QR_AVAILABLE:
        return None

    scale = max(1.0, float(ratio))
    key = (text, size, dark, light, scale)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        code = segno.make(text, error="m")
        # border=4 is the spec's quiet zone; segno's default already is 4 for
        # a full QR, but say it so a future edit cannot quietly drop it.
        matrix = list(code.matrix_iter(scale=1, border=4))
    except Exception:
        return None

    modules = len(matrix)
    if modules <= 0:
        return None

    # The module size is chosen in DEVICE pixels so every module lands on a
    # whole one, then converted back to logical units for painting: a pixmap
    # carrying a device pixel ratio makes QPainter scale by that ratio itself,
    # so passing device coordinates here would apply it twice.
    device = max(1, round(size * scale))
    device_module = max(1, device // modules)
    module_px = device_module / scale
    drawn = module_px * modules
    offset = (device / scale - drawn) / 2.0

    pixmap = QPixmap(device, device)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(QColor(light))

    painter = QPainter(pixmap)
    try:
        # No antialiasing on purpose: soft module edges are the single most
        # common reason a rendered QR fails to decode.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(dark))

        for row_index, row in enumerate(matrix):
            column = 0
            while column < modules:
                if not row[column]:
                    column += 1
                    continue

                # Draw each run of dark modules as one rectangle rather than
                # one per module: same pixels, a fraction of the calls.
                run = column
                while run < modules and row[run]:
                    run += 1

                painter.drawRect(
                    QRectF(
                        offset + column * module_px,
                        offset + row_index * module_px,
                        (run - column) * module_px,
                        module_px,
                    )
                )
                column = run
    finally:
        painter.end()

    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = pixmap
    return pixmap
