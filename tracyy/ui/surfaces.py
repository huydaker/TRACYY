"""Raised (neumorphic) tiles, composited rather than styled.

Qt stylesheets have no ``box-shadow``, and ``QGraphicsDropShadowEffect`` gives a
widget exactly one shadow — a raised tile needs two, a light one up-left and a
dark one down-right, or it reads flat. So the surface is composited here
instead: a rounded-rect mask, two blurred copies of it offset in opposite
directions, and a face gradient on top.

The blur is a three-pass separable box blur, which converges on a gaussian and
costs a handful of numpy operations. Every tile is cached by geometry and
state, so the cost is paid once per size, not once per repaint — this
application already budgets its idle redraw time carefully (see
``tracyy.core.perf``) and a per-frame blur would spend it all.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QAbstractButton, QToolButton

__all__ = [
    "RaisedIconButton",
    "TileStyle",
    "ToggleSwitch",
    "clear_tile_cache",
    "glow_icon",
    "raised_tile",
]


class TileStyle:
    """Colours for one tile family. Values are 0..1 strengths, not colours."""

    __slots__ = (
        "dark_strength",
        "face_bottom",
        "face_top",
        "light_strength",
        "offset",
        "spread",
    )

    def __init__(
        self,
        face_top: str = "#1F242A",
        face_bottom: str = "#171A1F",
        light_strength: float = 0.075,
        dark_strength: float = 0.66,
        offset: float = 3.0,
        spread: float = 5.0,
    ) -> None:
        self.face_top = face_top
        self.face_bottom = face_bottom
        self.light_strength = light_strength
        self.dark_strength = dark_strength
        self.offset = offset
        self.spread = spread


_CACHE: dict[tuple, QPixmap] = {}

#: Beyond this the cache is a leak rather than an optimisation; tile sizes are
#: fixed by layout, so a real application never approaches it.
_CACHE_LIMIT = 64


def clear_tile_cache() -> None:
    _CACHE.clear()


def _box_blur(values: np.ndarray, radius: int) -> np.ndarray:
    """Three box passes ≈ one gaussian, at a fraction of the arithmetic."""
    if radius < 1:
        return values

    result = values
    for _ in range(3):
        result = _box_blur_axis(result, radius, axis=1)
        result = _box_blur_axis(result, radius, axis=0)
    return result


def _box_blur_axis(values: np.ndarray, radius: int, axis: int) -> np.ndarray:
    if axis == 0:
        return _box_blur_axis(values.T, radius, axis=1).T

    height = values.shape[0]
    window = 2 * radius + 1
    padded = np.pad(values, ((0, 0), (radius, radius)), mode="constant")
    sums = np.cumsum(padded, axis=1, dtype=np.float32)
    sums = np.concatenate(
        (np.zeros((height, 1), dtype=np.float32), sums),
        axis=1,
    )
    return (sums[:, window:] - sums[:, :-window]) / float(window)


def _rounded_mask(
    width: int,
    height: int,
    rect: QRectF,
    radius: float,
) -> np.ndarray:
    """Antialiased 0..1 coverage of one rounded rectangle."""
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255))
        painter.drawRoundedRect(rect, radius, radius)
    finally:
        painter.end()

    stride = image.bytesPerLine() // 4
    buffer = np.frombuffer(image.constBits(), dtype=np.uint8)
    pixels = buffer.reshape(height, stride, 4)[:, :width, :]
    return pixels[:, :, 3].astype(np.float32) / 255.0


def _rounded_outline(
    width: int,
    height: int,
    rect: QRectF,
    radius: float,
    pen: float,
) -> np.ndarray:
    """Coverage of just the tile's edge, as a stroked rounded rectangle."""
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255), pen))
        painter.drawRoundedRect(rect, radius, radius)
    finally:
        painter.end()

    stride = image.bytesPerLine() // 4
    buffer = np.frombuffer(image.constBits(), dtype=np.uint8)
    pixels = buffer.reshape(height, stride, 4)[:, :width, :]
    return pixels[:, :, 3].astype(np.float32) / 255.0


def _shift(values: np.ndarray, dx: int, dy: int) -> np.ndarray:
    out = np.zeros_like(values)
    height, width = values.shape

    src_y0, dst_y0 = (0, dy) if dy >= 0 else (-dy, 0)
    src_x0, dst_x0 = (0, dx) if dx >= 0 else (-dx, 0)
    rows = height - abs(dy)
    cols = width - abs(dx)
    if rows <= 0 or cols <= 0:
        return out

    out[dst_y0 : dst_y0 + rows, dst_x0 : dst_x0 + cols] = values[
        src_y0 : src_y0 + rows, src_x0 : src_x0 + cols
    ]
    return out


def _over(base: np.ndarray, colour: tuple[float, float, float], alpha: np.ndarray) -> None:
    """Composite a flat colour over ``base`` (H, W, 4 float RGBA), in place."""
    src_a = np.clip(alpha, 0.0, 1.0)[:, :, None]
    src_rgb = np.array(colour, dtype=np.float32)[None, None, :] / 255.0

    dst_a = base[:, :, 3:4]
    out_a = src_a + dst_a * (1.0 - src_a)

    safe = np.where(out_a > 1e-6, out_a, 1.0)
    base[:, :, :3] = (
        src_rgb * src_a + base[:, :, :3] * dst_a * (1.0 - src_a)
    ) / safe
    base[:, :, 3:4] = out_a


def raised_tile(
    width: int,
    height: int,
    radius: float,
    *,
    pressed: bool = False,
    lift: float = 0.0,
    rim: float = 0.0,
    rim_colour: str = "#46DC00",
    style: TileStyle | None = None,
    ratio: float = 2.0,
) -> QPixmap:
    """One tile, ``width`` x ``height`` logical pixels, shadows included.

    ``lift`` (0..1) brightens the highlight and deepens the shadow, which is
    what the hover animation drives. It is quantised into the cache key so a
    smooth animation still reuses a small number of pixmaps.
    """
    style = style or TileStyle()
    scale = max(1.0, float(ratio))
    step = round(max(0.0, min(1.0, lift)) * 8.0) / 8.0

    rim_step = round(max(0.0, min(1.0, rim)) * 8.0) / 8.0
    key = (width, height, radius, pressed, step, rim_step, rim_colour, scale, id(style))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    px_w = max(1, round(width * scale))
    px_h = max(1, round(height * scale))

    margin = (style.offset + style.spread) * scale
    tile = QRectF(margin, margin, px_w - 2 * margin, px_h - 2 * margin)
    if tile.width() <= 1 or tile.height() <= 1:
        # Nothing left to draw once the shadow margin is taken out.
        pixmap = QPixmap(px_w, px_h)
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        return pixmap

    mask = _rounded_mask(px_w, px_h, tile, radius * scale)
    offset = max(1, round(style.offset * scale))
    blur = max(1, round(style.spread * scale * 0.6))

    canvas = np.zeros((px_h, px_w, 4), dtype=np.float32)

    light = style.light_strength * (1.0 + 0.55 * step)
    dark = style.dark_strength * (1.0 + 0.25 * step)

    if pressed:
        # Inset: the shadows fall inside the tile, so the face goes down first
        # and the blurred inverse mask is clipped back to it.
        _paint_face(canvas, mask, style)
        inverse = 1.0 - mask
        inner_dark = _box_blur(_shift(inverse, offset, offset), blur) * mask
        inner_light = _box_blur(_shift(inverse, -offset, -offset), blur) * mask
        _over(canvas, (0.0, 0.0, 0.0), inner_dark * dark)
        _over(canvas, (255.0, 255.0, 255.0), inner_light * light)
    else:
        outside = 1.0 - mask
        drop = _box_blur(_shift(mask, offset, offset), blur) * outside
        glow = _box_blur(_shift(mask, -offset, -offset), blur) * outside
        _over(canvas, (0.0, 0.0, 0.0), drop * dark)
        _over(canvas, (255.0, 255.0, 255.0), glow * light)
        _paint_face(canvas, mask, style)

    if rim_step > 0.0:
        # Two strokes of the same edge: a tight one that draws the line itself
        # and a wide one that lets it bleed a little past the tile. Together
        # they read as the rim being lit, not as a border being painted on.
        edge = _rounded_outline(px_w, px_h, tile, radius * scale, 1.1 * scale)
        colour = _rgb(rim_colour)
        _over(canvas, colour, _normalised_blur(edge, 4.0 * scale) * (0.17 * rim_step))
        _over(canvas, colour, _normalised_blur(edge, 1.2 * scale) * (0.30 * rim_step))

    pixmap = _to_pixmap(canvas, scale)
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = pixmap
    return pixmap


def _paint_face(canvas: np.ndarray, mask: np.ndarray, style: TileStyle) -> None:
    """Vertical gradient face, so the tile catches light from above."""
    top = QColor(style.face_top)
    bottom = QColor(style.face_bottom)
    height = canvas.shape[0]

    ramp = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    red = top.red() + (bottom.red() - top.red()) * ramp
    green = top.green() + (bottom.green() - top.green()) * ramp
    blue = top.blue() + (bottom.blue() - top.blue()) * ramp

    face = np.stack(
        (
            np.broadcast_to(red, mask.shape),
            np.broadcast_to(green, mask.shape),
            np.broadcast_to(blue, mask.shape),
        ),
        axis=2,
    ).astype(np.float32) / 255.0

    src_a = mask[:, :, None]
    dst_a = canvas[:, :, 3:4]
    out_a = src_a + dst_a * (1.0 - src_a)
    safe = np.where(out_a > 1e-6, out_a, 1.0)
    canvas[:, :, :3] = (face * src_a + canvas[:, :, :3] * dst_a * (1.0 - src_a)) / safe
    canvas[:, :, 3:4] = out_a


def _to_pixmap(canvas: np.ndarray, scale: float) -> QPixmap:
    height, width, _ = canvas.shape
    rgba = np.clip(canvas, 0.0, 1.0)

    # Qt's ARGB32 is BGRA in memory on little-endian, and premultiplied wants
    # the colour channels already scaled by alpha.
    alpha = rgba[:, :, 3:4]
    out = np.empty((height, width, 4), dtype=np.uint8)
    out[:, :, 0] = (rgba[:, :, 2] * alpha[:, :, 0] * 255.0).astype(np.uint8)
    out[:, :, 1] = (rgba[:, :, 1] * alpha[:, :, 0] * 255.0).astype(np.uint8)
    out[:, :, 2] = (rgba[:, :, 0] * alpha[:, :, 0] * 255.0).astype(np.uint8)
    out[:, :, 3] = (alpha[:, :, 0] * 255.0).astype(np.uint8)

    contiguous = np.ascontiguousarray(out)
    image = QImage(
        contiguous.data,
        width,
        height,
        width * 4,
        QImage.Format.Format_ARGB32_Premultiplied,
    ).copy()

    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(scale)
    return pixmap


_GLOW_CACHE: dict[tuple, QPixmap] = {}


def _rgb(colour: str) -> tuple[float, float, float]:
    value = QColor(colour)
    return (float(value.red()), float(value.green()), float(value.blue()))


def _normalised_blur(mask: np.ndarray, radius: float) -> np.ndarray:
    """Blur peaked back to 1.0, so each pass keeps its own falloff shape."""
    blurred = _box_blur(mask, max(1, round(radius)))
    peak = float(blurred.max())
    return blurred / peak if peak > 1e-6 else blurred


def glow_icon(
    icon,
    size: int,
    colour: str,
    *,
    glow_colour: str = "#46DC00",
    core_colour: str = "#FFFFFF",
    strength: float = 0.0,
    radius: float = 6.0,
    ratio: float = 2.0,
) -> QPixmap:
    """One glyph, recoloured, with a two-stage lamp halo behind it.

    The halo is built from two blurs of the same shape rather than one: a wide
    soft pass in ``glow_colour`` underneath, and a tight bright pass in
    ``core_colour`` on top. Where they overlap — right around the glyph — the
    bright one wins and the light reads white; further out only the wide pass
    survives and it falls off through the accent. That is how a real lamp
    behaves, and a single-colour halo cannot imitate it.

    Only the icon's alpha is used, so a single drawn glyph serves every state
    and the colour is decided here — a QIcon built from a bitmap cannot be
    tinted, which is why the nav glyphs could not simply be brightened before.

    ``strength`` (0..1) drives both passes, animated by hover and pinned high
    on the active page. At 0 nothing is blurred at all, so an idle window costs
    nothing extra.
    """
    scale = max(1.0, float(ratio))
    step = round(max(0.0, min(1.0, strength)) * 8.0) / 8.0
    key = (icon.cacheKey(), size, colour, glow_colour, core_colour, step, radius, scale)

    cached = _GLOW_CACHE.get(key)
    if cached is not None:
        return cached

    px = max(1, round(size * scale))
    # Room for the wider of the two passes, or it clips into a square.
    pad = max(1, round(radius * scale * 3.0))
    width = px + 2 * pad

    source = QImage(width, width, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(Qt.GlobalColor.transparent)
    painter = QPainter(source)
    try:
        icon.paint(painter, pad, pad, px, px)
    finally:
        painter.end()

    stride = source.bytesPerLine() // 4
    buffer = np.frombuffer(source.constBits(), dtype=np.uint8)
    mask = (
        buffer.reshape(width, stride, 4)[:, :width, 3].astype(np.float32) / 255.0
    )

    canvas = np.zeros((width, width, 4), dtype=np.float32)

    if step > 0.0:
        # White only. The accent moved out to the tile's rim, where it reads
        # as the button being lit rather than as coloured fog around a glyph.
        _over(
            canvas,
            _rgb(core_colour),
            _normalised_blur(mask, radius * scale * 0.55) * (0.42 * step),
        )

    face = QColor(colour)
    _over(canvas, (float(face.red()), float(face.green()), float(face.blue())), mask)

    pixmap = _to_pixmap(canvas, scale)
    if len(_GLOW_CACHE) >= _CACHE_LIMIT:
        _GLOW_CACHE.clear()
    _GLOW_CACHE[key] = pixmap
    return pixmap


class RaisedIconButton(QToolButton):
    """A tool button drawn as a raised tile, inset while it is the active page.

    The widget is deliberately larger than the tile: the shadow needs room
    outside the face, and a tile drawn to the widget edge has nowhere to cast
    one. ``tile`` is the visible size; the margin is derived from the style.

    Hover animates ``lift``, which brightens the highlight and deepens the
    shadow rather than moving anything. Motion in a show tool should read as
    the surface catching more light, not as the control jumping under a hand
    that is about to click it.
    """

    #: Lamp tones. Idle is dim enough to recede, lit is the page you are on.
    LAMP_OFF = "#7C848F"
    LAMP_HOVER = "#DCE4EC"
    LAMP_ON = "#FFFFFF"
    LAMP_GLOW = "#46DC00"
    LAMP_CORE = "#FFFFFF"

    def __init__(
        self,
        parent=None,
        *,
        tile: int = 44,
        radius: float = 14.0,
        style: TileStyle | None = None,
    ) -> None:
        super().__init__(parent)
        self._tile_style = style or TileStyle()
        self._glow_radius = 7.0
        self._tile_size = int(tile)
        self._radius = float(radius)
        self._lift = 0.0

        # The shadow reaches offset+spread past the face; anything more is
        # dead space the layout has to find room for.
        margin = round(self._tile_style.offset + self._tile_style.spread)
        extent = self._tile_size + 2 * margin
        self.setFixedSize(extent, extent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        self._animation = QPropertyAnimation(self, b"lift", self)
        self._animation.setDuration(150)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def get_lift(self) -> float:
        return self._lift

    def set_lift(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._lift) < 1e-4:
            return
        self._lift = value
        self.update()

    lift = Property(float, get_lift, set_lift)

    def _animate_to(self, target: float) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._lift)
        self._animation.setEndValue(float(target))
        self._animation.start()

    def enterEvent(self, event) -> None:
        self._animate_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._animate_to(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        del event

        try:
            ratio = max(1.0, float(self.devicePixelRatioF()))
        except Exception:
            ratio = 2.0

        # Selected pages read as pressed in. That is the neumorphic convention
        # and it needs no second colour to say "you are here".
        sunken = bool(self.isDown() or self.isChecked())

        # Hover is a hint, not an answer: it has to stay visibly below the lit
        # state or the rail stops telling you which page you are actually on.
        lamp = max(self._lift * 0.40, 1.0 if sunken else 0.0)

        pixmap = raised_tile(
            self.width(),
            self.height(),
            self._radius,
            pressed=sunken,
            lift=self._lift,
            rim=lamp,
            rim_colour=self.LAMP_GLOW,
            style=self._tile_style,
            ratio=ratio,
        )

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(0, 0, pixmap)

            icon = self.icon()
            if not icon.isNull():
                # Three states, one lamp: dim while idle, warming under the
                # cursor, lit on the page you are actually on. The glyph itself
                # is drawn once in grey and recoloured here — a QIcon made from
                # a pixmap cannot be tinted, so brightness has to be painted.
                tone = self.LAMP_ON if sunken else self.LAMP_OFF
                if not sunken and self._lift > 0.0:
                    tone = self.LAMP_HOVER

                size = self.iconSize()
                lamp = glow_icon(
                    icon,
                    size.width(),
                    tone,
                    glow_colour=self.LAMP_GLOW,
                    core_colour=self.LAMP_CORE,
                    strength=lamp,
                    radius=self._glow_radius,
                    ratio=ratio,
                )

                left = (self.width() - lamp.width() / ratio) / 2.0
                top = (self.height() - lamp.height() / ratio) / 2.0
                # A pressed tile carries its content down with it, by a pixel.
                if sunken:
                    top += 1.0
                painter.drawPixmap(QRectF(left, top, 0, 0).topLeft().toPoint(), lamp)
        finally:
            painter.end()


class ToggleSwitch(QAbstractButton):
    """A sliding on/off switch.

    Qt ships no such control, and a pair of Start/Stop buttons says less than
    one switch does: two buttons show you the two things you could do and make
    you work out which one is live from their enabled states, while a switch
    shows you the state itself and moving it is the action. It also halves the
    table columns it lives in.

    The knob position is animated rather than jumped, because the movement is
    what tells you which way the state went — on a table row that redraws
    wholesale, a switch that simply appears in the other position reads as a
    repaint, not as something you did.
    """

    TRACK_OFF = "#262B32"
    TRACK_ON = "#2E8B00"
    TRACK_BORDER_OFF = "#333941"
    TRACK_BORDER_ON = "#46DC00"
    KNOB = "#E9EEF4"
    KNOB_DISABLED = "#6B7280"

    def __init__(self, parent=None, *, width: int = 52, height: int = 28) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(width, height)

        self._travel = 1.0 if self.isChecked() else 0.0
        self._animation = QPropertyAnimation(self, b"travel", self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._on_toggled)

    def get_travel(self) -> float:
        return self._travel

    def set_travel(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._travel) < 1e-4:
            return
        self._travel = value
        self.update()

    travel = Property(float, get_travel, set_travel)

    def _on_toggled(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._travel)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def set_checked_silently(self, checked: bool) -> None:
        """Adopt a state without animating or emitting.

        The server table is rebuilt from scratch whenever anything changes, so
        the switch is told the state it already shows several times a second.
        Animating that would make it twitch, and emitting would start a loop
        back into the thing that rebuilt it.
        """
        self.blockSignals(True)
        self.setChecked(bool(checked))
        self.blockSignals(False)
        self._animation.stop()
        self._travel = 1.0 if checked else 0.0
        self.update()

    def sizeHint(self):
        return self.size()

    def paintEvent(self, event) -> None:
        del event

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            enabled = self.isEnabled()
            radius = self.height() / 2.0
            track = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)

            start = QColor(self.TRACK_OFF)
            end = QColor(self.TRACK_ON)
            fill = QColor(
                round(start.red() + (end.red() - start.red()) * self._travel),
                round(start.green() + (end.green() - start.green()) * self._travel),
                round(start.blue() + (end.blue() - start.blue()) * self._travel),
            )
            edge = QColor(self.TRACK_BORDER_ON if self._travel > 0.5 else self.TRACK_BORDER_OFF)
            if not enabled:
                fill.setAlpha(110)
                edge.setAlpha(110)

            painter.setPen(QPen(edge, 1.0))
            painter.setBrush(fill)
            painter.drawRoundedRect(track, radius, radius)

            inset = 3.0
            diameter = self.height() - 2 * inset
            span = self.width() - 2 * inset - diameter
            knob = QRectF(
                inset + span * self._travel,
                inset,
                diameter,
                diameter,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.KNOB if enabled else self.KNOB_DISABLED))
            painter.drawEllipse(knob)
        finally:
            painter.end()
