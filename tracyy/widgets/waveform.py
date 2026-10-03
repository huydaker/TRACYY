from __future__ import annotations

import math
import os
import time
from bisect import bisect_left, bisect_right
from collections import OrderedDict

import numpy as np
from PySide6.QtCore import QLineF, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from tracyy.core.ids import cue_key
from tracyy.gpu import GPU_AVAILABLE, GPU_BACKEND_NAME, GpuWaveformSurface
from tracyy.waveform import build_waveform_pyramid, select_lod_level

from .constants import normalize_marker_shape

# V1.2.1: prefer Qt Quick scene graph. On macOS this requests Metal.
# The old QOpenGLWidget/QPainter renderer remains as an automatic fallback.
_OPENGL_BACKEND = False
if GPU_AVAILABLE and os.environ.get("TRACYY_WAVEFORM_SOFTWARE", "0") != "1":
    QOpenGLWidget = None
    _WaveformWidgetBase = QWidget
else:
    try:
        if os.environ.get("TRACYY_WAVEFORM_SOFTWARE", "0") != "1":
            from PySide6.QtOpenGLWidgets import QOpenGLWidget

            _WaveformWidgetBase = QOpenGLWidget
            _OPENGL_BACKEND = True
        else:
            QOpenGLWidget = None
            _WaveformWidgetBase = QWidget
    except Exception:
        QOpenGLWidget = None
        _WaveformWidgetBase = QWidget


#: How often the presence marks are redrawn while they are catching up. Fast
#: enough to read as motion, slow enough to be nothing next to the waveform.
_PEER_FRAME_MS = 33

#: How long a mark may keep moving on its last known speed after the machine
#: went quiet. Past this it stops where it is: a laptop that stopped sending
#: has stopped, and a line still travelling across the song would be a claim
#: nobody made.
_PEER_COAST_SECONDS = 2.5

#: Fastest a mark may be assumed to travel, as a multiple of playback. Well
#: past anything real, and a hard stop on one bad measurement sending a line
#: off the end of the song.
_PEER_MAX_RATE = 4.0

#: A jump further than this many seconds of song is a seek, not playback: it
#: is shown at once rather than slid to, because sliding would draw somebody
#: travelling through a part of the show they never touched.
#:
#: In seconds rather than in a fraction of the track, which is the mistake
#: this replaced: one beat of playback is 1.5 seconds either way, but as a
#: fraction that is a twentieth of a short song and a hundredth of a long
#: one — so a fraction threshold called normal playback a seek on anything
#: under a couple of minutes.
_PEER_SNAP_SECONDS = 6.0

#: Used only when the track length is unknown, which is the moment before the
#: first track is open. Deliberately generous: snapping a mark that should
#: have glided is a smaller error than gliding one that should have snapped.
_PEER_FALLBACK_DURATION = 240.0


class _WaveformGpuOverlay(QWidget):
    """Tiny CPU overlay: only cue handles remain in QWidget/QPainter.

    Waveform bars, cue verticals, played mask and playhead live in Qt Quick/Metal.
    The overlay is transparent to mouse input so WaveformWidget keeps all of its
    existing scrub/zoom/marker-drag behavior.

    It is translucent and sits on top of a surface that re-renders on every
    playhead update, so Qt has to repaint it at the full refresh rate to
    recomposite. Rasterizing the handles on each of those repaints costs one
    QPainter shape per cue per frame, which on a dense show is by far the most
    expensive thing Tracyy does -- a 1553 cue track measured 17 ms per frame,
    burning CPU continuously without ever dropping one. The handles hold still
    while the transport runs, so they are rasterized once and then blitted.
    """

    def __init__(self, owner: WaveformWidget) -> None:
        super().__init__(owner)
        self.owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._handle_cache = QPixmap()
        self._handle_cache_key: tuple | None = None

    def _cached_handles(self) -> QPixmap:
        owner = self.owner
        pixel_ratio = float(self.devicePixelRatioF())

        # Everything the handles are drawn from. Marker edits, track changes
        # and marker drags all bump the revision, so nothing else is needed.
        key = (
            owner._markers_revision,
            round(float(owner.view_start), 9),
            round(float(owner.view_span), 9),
            self.width(),
            self.height(),
            round(pixel_ratio, 4),
        )
        if key == self._handle_cache_key and not self._handle_cache.isNull():
            return self._handle_cache

        pixmap = QPixmap(
            max(1, round(self.width() * pixel_ratio)),
            max(1, round(self.height() * pixel_ratio)),
        )
        pixmap.setDevicePixelRatio(pixel_ratio)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        try:
            owner._paint_gpu_marker_handles(painter)
        finally:
            painter.end()

        self._handle_cache = pixmap
        self._handle_cache_key = key
        return pixmap

    def paintEvent(self, event) -> None:
        if self.width() <= 0 or self.height() <= 0:
            return

        painter = QPainter(self)
        try:
            painter.setClipRegion(event.region())
        except Exception:
            pass
        painter.drawPixmap(0, 0, self._cached_handles())

        # Presence rides on top of the cached handles rather than inside them:
        # it moves on the network tick, and that cache has to keep its key on
        # the marker revision alone.
        owner = self.owner
        if owner._peer_marks:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            owner._paint_peer_presence(painter, owner.wave_rect())


class WaveformWidget(_WaveformWidgetBase):
    seek_requested = Signal(float)
    view_changed = Signal(float, float)
    marker_move_finished = Signal(str, float)

    def __init__(self) -> None:
        super().__init__()

        self.title = "No track selected"
        self.samples = np.zeros(
            600,
            dtype=np.float32,
        )
        self.progress = 0.0
        self.markers: list[
            tuple[
                int,
                float,
                str,
                str,
                bool,
            ]
        ] = []
        self._marker_positions: list[float] = []
        self._wave_lines_cache: list[QLineF] = []
        self._wave_lines_cache_key: tuple | None = None
        self._wave_pixmap_cache = QPixmap()
        self._wave_pixmap_cache_key: tuple | None = None
        self._samples_revision = 0

        # DJ-style multi-resolution renderer. Level 0 is the high-detail
        # gray amplitude preview; lower-detail levels are generated with cheap
        # pairwise pooling. Only visible 512-point tiles are rasterized.
        self._wave_pyramid: tuple[np.ndarray, ...] = ()
        self._wave_tile_size = 512
        self._wave_tile_cache: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._wave_tile_cache_limit = 72
        self._last_lod_level = 0
        self._last_progress_for_dirty_update = 0.0

        if _OPENGL_BACKEND:
            try:
                self.setUpdateBehavior(QOpenGLWidget.UpdateBehavior.PartialUpdate)
            except Exception:
                pass

        self._gpu_surface: GpuWaveformSurface | None = None
        self._gpu_overlay: _WaveformGpuOverlay | None = None
        self._gpu_backend_name = "QPainter fallback"
        self._markers_revision = 0
        # Cropping the peak envelope and building the cue list is the
        # expensive half of a GPU sync. paintEvent calls it on every repaint,
        # so the view state it was last built for is remembered and an
        # unchanged view returns immediately.
        self._gpu_static_view_key: tuple | None = None

        self.view_start = 0.0
        self.view_span = 1.0
        self.min_view_span = 0.001

        #: Where the other machines in the session are, as
        #: ``(colour, name, position)`` with position in the same 0..1 space
        #: as ``progress``. Deliberately not part of ``markers``: presence
        #: moves several times a second and markers do not, and the GPU handle
        #: cache is keyed on the marker revision — putting presence in there
        #: would rebuild that pixmap on every tick, which is the one thing
        #: that cache exists to prevent.
        self._peer_marks: tuple[tuple[str, str, float], ...] = ()
        #: machine key -> where it says it is, how fast, and where it is being
        #: drawn while the two catch up.
        self._peer_motion: dict[str, dict] = {}
        self._peer_motion_at = 0.0
        self._peer_timer = QTimer(self)
        self._peer_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._peer_timer.timeout.connect(self._advance_peer_motion)

        self._scrubbing = False
        # Cleared while Input Timecode Chase owns the timeline, alongside the
        # transport buttons and the position slider.
        self.scrub_enabled = True
        # Dragging the playhead must not seek the transport once per mouse
        # event: every Transport.seek() tears down the streaming decoder
        # process and starts a new one, which blocks this thread for roughly a
        # third of a second. The drawn playhead follows the pointer directly
        # and the transport is asked to catch up on this interval, measured
        # from the end of the previous seek, plus a final one on release.
        self._scrub_position: float | None = None
        self._scrub_seek_interval = 0.12
        self._scrub_last_seek_at = 0.0
        self._scrub_seek_timer = QTimer(self)
        self._scrub_seek_timer.setSingleShot(True)
        self._scrub_seek_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._scrub_seek_timer.timeout.connect(self._flush_scrub_seek)
        self._panning = False
        self._dragging_marker_id: int | None = None
        self._dragging_marker_position = 0.0
        self._marker_drag_started = False
        self._press_mouse_x = 0.0
        self._press_mouse_y = 0.0
        self._last_mouse_x = 0.0

        # Marker line is display-only. Only this small top handle is interactive.
        self._marker_handle_width = 10.0
        self._marker_handle_height = 12.0
        self._marker_visual_size = 10.0

        self.auto_follow = True
        # Fixed page-follow behavior: the waveform stays still while the
        # playhead moves. The visible page changes only when the playhead
        # reaches 80% of the current view.
        self.page_follow_trigger = 0.80
        self.page_follow_return = 0.15
        self.page_follow_back_trigger = 0.05
        self._manual_hold_until = 0.0

        # Short transition used only during a page jump; this is not
        # continuous waveform-follow.
        self._page_follow_animation_timer = QTimer(self)
        self._page_follow_animation_timer.setInterval(16)
        self._page_follow_animation_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._page_follow_animation_timer.timeout.connect(self._advance_page_follow_animation)
        self._page_follow_animation_start = 0.0
        self._page_follow_animation_target = 0.0
        self._page_follow_animation_started_at = 0.0
        self._page_follow_animation_duration = 0.18

        self.setMinimumHeight(170)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip(
            "Trackpad: two-finger vertical scroll = zoom; "
            "two-finger horizontal scroll = pan. "
            "Click or hold-drag anywhere on the waveform = move playhead. "
            "Hold-drag only the small marker handle at the top = move marker."
        )

        # GPU renderer is deliberately optional. A Qt Quick or Metal failure
        # must never prevent Tracyy from opening a show.
        if GPU_AVAILABLE and os.environ.get("TRACYY_WAVEFORM_SOFTWARE", "0") != "1":
            try:
                self._gpu_surface = GpuWaveformSurface(self)
                self._gpu_overlay = _WaveformGpuOverlay(self)
                self._gpu_backend_name = GPU_BACKEND_NAME
                self._layout_gpu_children()
                self._gpu_overlay.raise_()
                self.setToolTip(self.toolTip() + f"\nRenderer: {self._gpu_backend_name}")
            except Exception:
                self._gpu_surface = None
                self._gpu_overlay = None
                self._gpu_backend_name = "QPainter fallback"

    def set_track(self, title: str, samples: np.ndarray) -> None:
        self.title = title

        preview = np.asarray(samples, dtype=np.float32)
        if preview.ndim == 2:
            # Compatibility with the previous spectral trial cache: only the
            # amplitude column is kept. Color/FFT features are intentionally
            # discarded in the gray waveform renderer.
            preview = preview[:, 0]
        self.samples = np.ascontiguousarray(
            preview.reshape(-1),
            dtype=np.float32,
        )

        self._samples_revision += 1
        self._wave_lines_cache_key = None
        self._wave_lines_cache = []
        self._wave_pixmap_cache_key = None
        self._wave_pixmap_cache = QPixmap()
        self._rebuild_waveform_pyramid()
        if self._gpu_surface is not None:
            self._gpu_surface.invalidate_static()
            self._gpu_static_view_key = None
        self.progress = 0.0
        self._last_progress_for_dirty_update = 0.0
        self.markers = []
        self._marker_positions = []
        self._markers_revision += 1
        self.fit_view()
        self.update()

    def set_markers(
        self,
        markers: list[
            tuple[
                int,
                float,
                str,
                str,
                bool,
                str,
            ]
        ],
    ) -> None:
        normalized_markers = []

        for marker in markers:
            if len(marker) >= 6:
                (
                    cue_id,
                    position,
                    label,
                    color,
                    locked,
                    marker_shape,
                ) = marker[:6]
            else:
                (
                    cue_id,
                    position,
                    label,
                    color,
                    locked,
                ) = marker[:5]
                marker_shape = "rectangle"

            normalized_markers.append(
                (
                    cue_key(cue_id),
                    max(
                        0.0,
                        min(
                            1.0,
                            float(position),
                        ),
                    ),
                    str(label),
                    str(color),
                    bool(locked),
                    normalize_marker_shape(marker_shape),
                )
            )

        self.markers = sorted(
            normalized_markers,
            key=lambda marker: marker[1],
        )
        self._marker_positions = [float(marker[1]) for marker in self.markers]
        self._markers_revision += 1
        if self._gpu_surface is not None:
            self._gpu_surface.invalidate_static()
            self._gpu_static_view_key = None
        self.update()

    def clear_track(self) -> None:
        self.title = "No track selected"
        self.samples = np.zeros(600, dtype=np.float32)
        self._samples_revision += 1
        self._wave_lines_cache_key = None
        self._wave_lines_cache = []
        self._wave_pixmap_cache_key = None
        self._wave_pixmap_cache = QPixmap()
        self._rebuild_waveform_pyramid()
        if self._gpu_surface is not None:
            self._gpu_surface.invalidate_static()
            self._gpu_static_view_key = None
        self.progress = 0.0
        self._last_progress_for_dirty_update = 0.0
        self.markers = []
        self._marker_positions = []
        self._markers_revision += 1
        self.fit_view()
        self.update()

    def set_peer_presence(self, marks, duration: float = 0.0) -> None:
        """Where everyone else is on this track, for the presence layer.

        Positions arrive on the network beat, which is far too slow to watch:
        drawn as they land, another machine's playhead hops forward once every
        beat and a step of that size reads as a fault rather than as playback.
        So each one is given a speed, measured from the two most recent
        positions, and drawn where it should be *now* — corrected toward the
        truth as each new position lands rather than jumping to it.

        Nothing here interpolates the local playhead, which is exact and needs
        no help, and nothing extrapolates for longer than
        :data:`_PEER_COAST_SECONDS`: a machine that stopped sending has
        stopped, and guessing further would walk a line across a song nobody
        is playing.
        """
        now = time.monotonic()
        seen = set()
        changed = False
        try:
            length = float(duration)
        except (TypeError, ValueError):
            length = 0.0
        if length <= 0.0:
            length = _PEER_FALLBACK_DURATION
        # Both thresholds live in fractions because that is what a position
        # is, but both are decided in seconds of song.
        snap = _PEER_SNAP_SECONDS / length
        max_speed = _PEER_MAX_RATE / length

        for mark in marks or ():
            try:
                target = float(mark.get("position", 0.0))
            except (TypeError, ValueError):
                continue
            if target != target:  # NaN, from a peer that sent nonsense
                continue

            colour = str(mark.get("colour") or "#4C8DF6")
            name = str(mark.get("name") or "")
            key = str(mark.get("id") or name or colour)
            seen.add(key)

            motion = self._peer_motion.get(key)
            if motion is None:
                # First sight of this machine: no speed to measure yet, so it
                # simply appears where it says it is.
                self._peer_motion[key] = {
                    "colour": colour, "name": name, "target": target,
                    "shown": target, "speed": 0.0, "at": now,
                }
                changed = True
                continue

            if target != motion["target"]:
                elapsed = max(1e-3, now - motion["at"])
                step = target - motion["target"]
                # Backwards means somebody seeked, and a seek has no speed;
                # forwards is playback, whose speed is worth carrying.
                speed = step / elapsed if step > 0 else 0.0
                motion["speed"] = max(0.0, min(speed, max_speed))
                motion["target"] = target
                motion["at"] = now
                if step < -snap or step > snap:
                    # Too far to glide: this is a seek, not playback, and
                    # sliding across half a song would be a lie about where
                    # they have been.
                    motion["shown"] = target
                changed = True

            motion["colour"] = colour
            motion["name"] = name

        for key in [key for key in self._peer_motion if key not in seen]:
            del self._peer_motion[key]
            changed = True

        if changed:
            self._sync_peer_marks()
            self._restart_peer_animation()

    def _peer_position_now(self, motion: dict, now: float) -> float:
        """Where this machine should be drawn at ``now``."""
        coasted = min(max(0.0, now - float(motion["at"])), _PEER_COAST_SECONDS)
        return float(motion["target"]) + float(motion["speed"]) * coasted

    def _advance_peer_motion(self) -> None:
        """Move every mark a little closer to where it should be."""
        now = time.monotonic()
        elapsed = max(0.0, now - self._peer_motion_at)
        self._peer_motion_at = now
        # A fixed fraction per second, so the catch-up looks the same whether
        # the timer is keeping up or the machine is busy drawing a show.
        blend = 1.0 - pow(0.02, min(0.25, elapsed))

        moving = False
        for motion in self._peer_motion.values():
            wanted = self._peer_position_now(motion, now)
            shown = float(motion["shown"])
            if abs(wanted - shown) > 1e-6:
                motion["shown"] = shown + (wanted - shown) * blend
                moving = True
            if motion["speed"] > 0.0 and now - float(motion["at"]) < _PEER_COAST_SECONDS:
                moving = True

        self._sync_peer_marks()
        if self._gpu_overlay is not None:
            self._gpu_overlay.update()
        self.update()
        if not moving:
            self._peer_timer.stop()

    def _restart_peer_animation(self) -> None:
        self._peer_motion_at = time.monotonic()
        if self._peer_motion and not self._peer_timer.isActive():
            self._peer_timer.start(_PEER_FRAME_MS)
        if not self._peer_motion:
            self._peer_timer.stop()
        if self._gpu_overlay is not None:
            self._gpu_overlay.update()
        self.update()

    def _sync_peer_marks(self) -> None:
        """The tuples the paint path reads, rebuilt from the motion state."""
        self._peer_marks = tuple(
            (str(motion["colour"]), str(motion["name"]), float(motion["shown"]))
            for motion in self._peer_motion.values()
        )

    def _paint_peer_presence(self, painter: QPainter, wave_rect: QRectF) -> None:
        """Draw the other machines as thin dashed playheads with a name.

        Deliberately unlike the local playhead: 1.1px dashed against 2.2px
        solid, with a dot on top. Somebody glancing down mid-show has to know
        which line is theirs without reading anything.
        """
        if not self._peer_marks or wave_rect.width() <= 0.0:
            return

        span = max(self.view_span, 1e-9)
        painter.save()
        font = painter.font()
        font.setPointSizeF(8.0)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        for colour_hex, name, position in self._peer_marks:
            visible = (position - self.view_start) / span
            if not 0.0 <= visible <= 1.0:
                # Outside the current view. The row above the waveform still
                # names them; drawing at the edge would claim a position they
                # are not at.
                continue
            colour = QColor(colour_hex)
            if not colour.isValid():
                colour = QColor("#4C8DF6")
            x = wave_rect.left() + wave_rect.width() * visible

            pen = QPen(colour, 1.1)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(QLineF(x, wave_rect.top() + 11.0, x, wave_rect.bottom()))

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawEllipse(QPointF(x, wave_rect.top() + 7.0), 3.0, 3.0)

            if not name:
                continue
            label = metrics.elidedText(name, Qt.TextElideMode.ElideRight, 96)
            width = metrics.horizontalAdvance(label) + 12.0
            left = x + 6.0
            if left + width > wave_rect.right():
                # Flip to the left of the line rather than run off the end.
                left = x - 6.0 - width
            tag = QRectF(left, wave_rect.top() + 15.0, width, 16.0)
            painter.setPen(QPen(colour, 0.8))
            painter.setBrush(QColor(9, 13, 6, 225))
            painter.drawRoundedRect(tag, 5.0, 5.0)
            painter.setPen(colour)
            painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, label)

        painter.restore()

    def set_progress(self, progress: float, playing: bool = False) -> None:
        if self._scrubbing and self._scrub_position is not None:
            if QApplication.mouseButtons() == Qt.MouseButton.NoButton:
                # No button is down, so the release never reached us: a modal
                # dialog took the grab, or the window was switched away
                # mid-drag. Recover here rather than trusting that every way
                # of losing a grab was foreseen.
                self._end_scrub(flush=False)
            else:
                # The drag owns the playhead. The transport is still one
                # throttled seek behind, and letting it write here would drag
                # the playhead back to the old position between mouse events.
                return

        self._apply_progress(progress, playing)

    def _apply_progress(self, progress: float, playing: bool = False) -> None:
        old_progress = self.progress
        old_view_start = self.view_start
        old_view_span = self.view_span

        value = max(0.0, min(1.0, float(progress)))
        self.progress = value

        if playing and self.auto_follow:
            self.follow_progress_smooth()

        # If page-follow changed the viewport, redraw the whole waveform. During
        # ordinary playback, only repaint the narrow strip between the old and
        # new playhead positions. The dimmed 'already played' boundary changes
        # only inside that strip, so the waveform tiles themselves stay cached.
        actual_viewport_changed = (
            abs(self.view_start - old_view_start) > 1.0e-12
            or abs(self.view_span - old_view_span) > 1.0e-12
        )
        viewport_changed = (
            actual_viewport_changed or self._page_follow_animation_timer.isActive()
        )

        if self._gpu_surface is not None:
            # Normal playback changes only two cheap scene-graph properties.
            # Static waveform/cue geometry is touched only if the viewport
            # actually moved; the page-jump timer repaints its own frames.
            if actual_viewport_changed and not self._page_follow_animation_timer.isActive():
                # While a page jump animates, its own timer already repaints
                # each frame. Rebuilding here as well would crop the peak
                # envelope twice for the same frame.
                self._sync_gpu_static()
            self._sync_gpu_progress()
            self._last_progress_for_dirty_update = self.progress
            return

        if viewport_changed:
            self.update()
        else:
            rect = self.wave_rect()
            span = max(self.view_span, 1.0e-9)
            old_visible = (old_progress - self.view_start) / span
            new_visible = (self.progress - self.view_start) / span

            if -0.05 <= old_visible <= 1.05 or -0.05 <= new_visible <= 1.05:
                old_x = rect.left() + rect.width() * old_visible
                new_x = rect.left() + rect.width() * new_visible
                left = math.floor(min(old_x, new_x) - 5.0)
                right = math.ceil(max(old_x, new_x) + 5.0)
                dirty = QRect(
                    left,
                    math.floor(rect.top()),
                    max(1, right - left + 1),
                    math.ceil(rect.height()) + 2,
                ).intersected(self.rect())
                if not dirty.isEmpty():
                    self.update(dirty)
                else:
                    self.update()
            else:
                self.update()

        self._last_progress_for_dirty_update = self.progress

    def _stop_page_follow_animation(self) -> None:
        if self._page_follow_animation_timer.isActive():
            self._page_follow_animation_timer.stop()

    def _start_page_follow_animation(
        self,
        target_start: float,
    ) -> None:
        target = max(
            0.0,
            min(
                1.0 - self.view_span,
                float(target_start),
            ),
        )
        if abs(target - self.view_start) < 1e-9:
            return

        self._page_follow_animation_start = self.view_start
        self._page_follow_animation_target = target
        self._page_follow_animation_started_at = time.perf_counter()
        self._page_follow_animation_timer.start()

    def _advance_page_follow_animation(self) -> None:
        elapsed = time.perf_counter() - self._page_follow_animation_started_at
        duration = max(
            0.01,
            self._page_follow_animation_duration,
        )
        ratio = max(
            0.0,
            min(1.0, elapsed / duration),
        )

        # Cubic ease-out: a quick page move with a soft landing.
        eased = 1.0 - (1.0 - ratio) ** 3
        self.view_start = (
            self._page_follow_animation_start
            + (self._page_follow_animation_target - self._page_follow_animation_start) * eased
        )
        self.view_changed.emit(
            self.view_start,
            self.view_span,
        )
        self.update()

        if ratio >= 1.0:
            self.view_start = self._page_follow_animation_target
            self._page_follow_animation_timer.stop()
            self.view_changed.emit(
                self.view_start,
                self.view_span,
            )
            self.update()

    def follow_progress_smooth(self) -> None:
        # Do not drag the waveform continuously. Jump page only at 80%.
        if not self.auto_follow or self.view_span >= 0.999:
            return

        if self._page_follow_animation_timer.isActive():
            return

        if self._scrubbing or self._panning or self._dragging_marker_id is not None:
            return

        if time.perf_counter() < self._manual_hold_until:
            return

        right_edge = self.view_start + self.page_follow_trigger * self.view_span
        left_edge = self.view_start + self.page_follow_back_trigger * self.view_span
        new_start: float | None = None

        if self.progress >= right_edge or self.progress < left_edge:
            new_start = self.progress - self.page_follow_return * self.view_span

        if new_start is None:
            return

        self._start_page_follow_animation(new_start)

    def fit_view(self) -> None:
        self._stop_page_follow_animation()
        self.view_start = 0.0
        self.view_span = 1.0
        self.view_changed.emit(self.view_start, self.view_span)
        self.update()

    def zoom_in(self) -> None:
        ratio = (self.progress - self.view_start) / max(self.view_span, 1e-9)
        self.zoom_at(max(0.0, min(1.0, ratio)), 0.5)

    def zoom_out(self) -> None:
        ratio = (self.progress - self.view_start) / max(self.view_span, 1e-9)
        self.zoom_at(max(0.0, min(1.0, ratio)), 2.0)

    def zoom_at(self, cursor_ratio: float, factor: float) -> None:
        self._stop_page_follow_animation()
        cursor_ratio = max(0.0, min(1.0, cursor_ratio))
        anchor = self.view_start + cursor_ratio * self.view_span

        new_span = max(
            self.min_view_span,
            min(1.0, self.view_span * factor),
        )
        new_start = anchor - cursor_ratio * new_span
        new_start = max(0.0, min(1.0 - new_span, new_start))

        self.view_start = new_start
        self.view_span = new_span
        self._manual_hold_until = time.perf_counter() + 1.0
        # The full-track peak pyramid is independent of the viewport, so zoom only
        # changes source coordinates and requests one lightweight repaint.
        self.view_changed.emit(self.view_start, self.view_span)
        self.update()

    @property
    def gpu_backend_name(self) -> str:
        return self._gpu_backend_name

    def _layout_gpu_children(self) -> None:
        if self._gpu_surface is None:
            return
        rect = self.wave_rect().toAlignedRect().intersected(self.rect())
        self._gpu_surface.setGeometry(rect)
        if self._gpu_overlay is not None:
            self._gpu_overlay.setGeometry(self.rect())
            self._gpu_overlay.raise_()

    def resizeEvent(self, event) -> None:
        try:
            super().resizeEvent(event)
        except Exception:
            pass
        if self._gpu_surface is not None:
            self._layout_gpu_children()
            self._gpu_surface.invalidate_static()
            self._gpu_static_view_key = None
            self._sync_gpu_static(force=True)
            self._sync_gpu_progress()

    def _gpu_visible_amplitudes(self) -> tuple[np.ndarray, tuple]:
        """Return a dense peak envelope for the Qt Quick/Metal surface.

        V1.2.3 selected an already-reduced pyramid level and then reduced it a
        second time for QML.  At some zoom levels that could leave only a few
        dozen samples across a wide widget, which the Canvas rendered as evenly
        spaced 1 px sticks.  The GPU surface is static between view changes, so
        it is cheap and much safer to always crop from the full-detail base
        level and peak-pool that crop directly to screen density.
        """
        rect = self.wave_rect()
        if not self._wave_pyramid or rect.width() <= 1.0:
            return np.zeros(1, dtype=np.float32), (self._samples_revision, 0)

        base = np.asarray(self._wave_pyramid[0], dtype=np.float32)
        values = base[:, 0] if base.ndim == 2 else base.reshape(-1)
        count = len(values)
        if count <= 0:
            return np.zeros(1, dtype=np.float32), (self._samples_revision, 0, 0)

        start = max(0, min(count - 1, math.floor(self.view_start * count)))
        end = max(
            start + 1,
            min(count, math.ceil((self.view_start + self.view_span) * count)),
        )
        source = np.ascontiguousarray(values[start:end], dtype=np.float32)

        # Roughly two peak bins per logical pixel gives a dense DJ-style
        # envelope while keeping the Python -> QML property payload bounded.
        target = max(384, min(4096, math.ceil(rect.width() * 2.0)))
        if len(source) > target:
            boundaries = np.floor(
                np.arange(target, dtype=np.float64) * (len(source) / float(target))
            ).astype(np.int64)
            boundaries = np.clip(boundaries, 0, len(source) - 1)
            visible = np.maximum.reduceat(source, boundaries)[:target]
        else:
            visible = source

        # Extremely deep zoom or unusually short preview data can contain fewer
        # bins than screen pixels.  Interpolate only for display density; source
        # peaks and timing stay untouched.  This prevents visible blank gaps.
        minimum_display_points = max(2, min(4096, math.ceil(rect.width())))
        if 1 < len(visible) < minimum_display_points:
            old_x = np.linspace(0.0, 1.0, len(visible), dtype=np.float64)
            new_x = np.linspace(0.0, 1.0, minimum_display_points, dtype=np.float64)
            visible = np.interp(new_x, old_x, visible).astype(np.float32)

        visible = np.clip(visible, 0.0, 1.0).astype(np.float32, copy=False)
        key = (
            self._samples_revision,
            0,  # GPU always crops the full-detail base level in V1.2.4.
            int(start),
            int(end),
            len(visible),
            round(rect.width()),
            round(rect.height()),
            round(float(self.view_start), 9),
            round(float(self.view_span), 9),
        )
        return np.ascontiguousarray(visible), key

    def _gpu_cue_lines(self) -> tuple[list[dict[str, object]], tuple]:
        cue_lines: list[dict[str, object]] = []
        marker_start = bisect_left(self._marker_positions, self.view_start)
        marker_end = bisect_right(
            self._marker_positions,
            self.view_start + self.view_span,
        )
        span = max(self.view_span, 1.0e-9)
        for (
            _cue_id,
            marker_position,
            _marker_label,
            marker_hex,
            marker_locked,
            _marker_shape,
        ) in self.markers[marker_start:marker_end]:
            cue_lines.append(
                {
                    "ratio": float((marker_position - self.view_start) / span),
                    "color": str(marker_hex or "#ffb020"),
                    "lineWidth": 3.2 if marker_locked else 2.2,
                }
            )
        key = (
            self._markers_revision,
            round(self.view_start, 8),
            round(self.view_span, 8),
            marker_start,
            marker_end,
        )
        return cue_lines, key

    def _disable_gpu_renderer(self) -> None:
        surface = self._gpu_surface
        overlay = self._gpu_overlay
        self._gpu_surface = None
        self._gpu_overlay = None
        self._gpu_backend_name = "QPainter fallback"
        try:
            tip = self.toolTip().split("\nRenderer:", 1)[0]
            self.setToolTip(tip + "\nRenderer: QPainter fallback")
        except Exception:
            pass
        for widget in (overlay, surface):
            if widget is None:
                continue
            try:
                widget.hide()
                widget.deleteLater()
            except Exception:
                pass
        self.update()

    def _sync_gpu_static(self, force: bool = False) -> None:
        if self._gpu_surface is None:
            return

        rect = self.wave_rect()
        view_key = (
            self._samples_revision,
            self._markers_revision,
            round(float(self.view_start), 9),
            round(float(self.view_span), 9),
            round(rect.width()),
            round(rect.height()),
        )
        if not force and view_key == self._gpu_static_view_key:
            return

        try:
            amplitudes, amplitude_key = self._gpu_visible_amplitudes()
            cues, cue_key = self._gpu_cue_lines()
            if force:
                self._gpu_surface.invalidate_static()
            self._gpu_static_view_key = None
            self._gpu_surface.set_amplitudes(amplitudes, amplitude_key)
            self._gpu_surface.set_cues(cues, cue_key)
            if self._gpu_overlay is not None:
                self._gpu_overlay.update()
            self._gpu_static_view_key = view_key
        except Exception:
            # Runtime scene-graph failure: immediately fall back to the original
            # QWidget renderer instead of leaving a blank waveform.
            self._disable_gpu_renderer()

    def _sync_gpu_progress(self) -> None:
        if self._gpu_surface is None:
            return
        ratio = (self.progress - self.view_start) / max(self.view_span, 1.0e-9)
        try:
            self._gpu_surface.set_progress_ratio(ratio)
        except Exception:
            self._disable_gpu_renderer()

    def _paint_gpu_marker_handles(self, painter: QPainter) -> None:
        if self._gpu_surface is None:
            return
        wave_rect = self.wave_rect()
        painter.save()
        painter.setClipRect(wave_rect)
        marker_start = bisect_left(self._marker_positions, self.view_start)
        marker_end = bisect_right(
            self._marker_positions,
            self.view_start + self.view_span,
        )
        for (
            _cue_id,
            marker_position,
            _marker_label,
            marker_hex,
            marker_locked,
            marker_shape,
        ) in self.markers[marker_start:marker_end]:
            visible_marker = (marker_position - self.view_start) / max(self.view_span, 1.0e-9)
            marker_x = wave_rect.left() + wave_rect.width() * visible_marker
            marker_color = QColor(marker_hex)
            if not marker_color.isValid():
                marker_color = QColor("#ffb020")
            handle_rect = QRectF(
                marker_x - self._marker_handle_width / 2.0,
                wave_rect.top(),
                self._marker_handle_width,
                self._marker_handle_height,
            )
            self.draw_marker_handle(
                painter,
                handle_rect,
                marker_color,
                marker_shape,
                marker_locked,
            )
        painter.restore()

    def wave_rect(self) -> QRectF:
        return QRectF(
            18,
            48,
            max(1, self.width() - 36),
            max(60, self.height() - 72),
        )

    def position_from_x(self, x: float) -> float:
        rect = self.wave_rect()
        local = (x - rect.left()) / max(1.0, rect.width())
        local = max(0.0, min(1.0, local))
        return self.view_start + local * self.view_span

    def seek_from_x(self, x: float) -> None:
        self.seek_requested.emit(self.position_from_x(x))

    def _apply_scrub_position(self, position: float) -> None:
        """Move the drawn playhead to the pointer, without touching audio."""
        value = max(0.0, min(1.0, float(position)))
        self._scrub_position = value
        self._apply_progress(value)

    def _request_scrub_seek(self, force: bool = False) -> None:
        if self._scrub_position is None:
            return

        remaining = self._scrub_seek_interval - (time.perf_counter() - self._scrub_last_seek_at)
        if force or remaining <= 0.0:
            self._scrub_seek_timer.stop()
            self._flush_scrub_seek()
            return

        if not self._scrub_seek_timer.isActive():
            self._scrub_seek_timer.start(int(remaining * 1000.0) + 1)

    def _end_scrub(self, flush: bool = True) -> None:
        """Hand the playhead back to the transport.

        set_progress() ignores the transport while a drag owns the playhead,
        so this has to run for every way a drag can end. Missing one would
        leave the playhead frozen for the rest of the session.
        """
        self._scrub_seek_timer.stop()
        if flush:
            # Land the transport exactly where the pointer was released, even
            # if the throttle swallowed the last few moves.
            self._request_scrub_seek(force=True)
        self._scrub_position = None
        self._scrubbing = False

    def _flush_scrub_seek(self) -> None:
        if self._scrub_position is None:
            return
        self.seek_requested.emit(self._scrub_position)
        # Timed from the end of the seek, so a slow one throttles itself
        # further instead of queueing up behind the pointer.
        self._scrub_last_seek_at = time.perf_counter()

    def marker_x(self, marker_position: float) -> float:
        visible = (marker_position - self.view_start) / max(self.view_span, 1e-9)
        return self.wave_rect().left() + self.wave_rect().width() * visible

    def marker_handle_rect(
        self,
        position: float,
    ) -> QRectF:
        rect = self.wave_rect()
        marker_x = self.marker_x(position)
        return QRectF(
            marker_x - self._marker_handle_width / 2.0,
            rect.top(),
            self._marker_handle_width,
            self._marker_handle_height,
        )

    def marker_handle_at_point(
        self,
        x: float,
        y: float,
    ) -> int | None:
        point_x = float(x)
        point_y = float(y)

        start = bisect_left(
            self._marker_positions,
            self.view_start,
        )
        end = bisect_right(
            self._marker_positions,
            self.view_start + self.view_span,
        )

        best_id: int | None = None
        best_distance = float("inf")

        for (
            cue_id,
            position,
            _label,
            _color,
            _locked,
            _marker_shape,
        ) in self.markers[start:end]:
            handle = self.marker_handle_rect(position)
            if not handle.contains(
                point_x,
                point_y,
            ):
                continue

            distance = abs(handle.center().x() - point_x)
            if distance < best_distance:
                best_distance = distance
                best_id = cue_id

        return best_id

    def marker_at_x(
        self,
        x: float,
        tolerance: float = 4.0,
    ) -> int | None:
        best_id = None
        best_distance = float(tolerance)

        start = bisect_left(
            self._marker_positions,
            self.view_start,
        )
        end = bisect_right(
            self._marker_positions,
            self.view_start + self.view_span,
        )

        for (
            cue_id,
            position,
            _label,
            _color,
            _locked,
            _marker_shape,
        ) in self.markers[start:end]:
            distance = abs(self.marker_x(position) - x)
            if distance <= best_distance:
                best_distance = distance
                best_id = cue_id

        return best_id

    def marker_is_locked(
        self,
        cue_id: int,
    ) -> bool:
        for (
            existing_id,
            _position,
            _label,
            _color,
            locked,
            _marker_shape,
        ) in self.markers:
            if existing_id == cue_id:
                return bool(locked)
        return False

    def update_local_marker_position(
        self,
        cue_id: int,
        position: float,
    ) -> None:
        updated = []
        normalized = max(
            0.0,
            min(
                1.0,
                float(position),
            ),
        )

        for marker in self.markers:
            (
                marker_id,
                marker_position,
                label,
                color,
                locked,
                marker_shape,
            ) = marker

            if marker_id == cue_id:
                marker_position = normalized

            updated.append(
                (
                    marker_id,
                    marker_position,
                    label,
                    color,
                    locked,
                    marker_shape,
                )
            )

        self.markers = sorted(
            updated,
            key=lambda marker: marker[1],
        )
        self._marker_positions = [float(marker[1]) for marker in self.markers]
        self._markers_revision += 1
        if self._gpu_surface is not None:
            self._gpu_surface.invalidate_static()
            self._gpu_static_view_key = None
        self.update()

    def pan_view_by_pixels(
        self,
        delta_pixels: float,
    ) -> None:
        if self.view_span >= 0.999999:
            return

        rect = self.wave_rect()
        delta_normalized = (
            float(delta_pixels)
            / max(
                1.0,
                rect.width(),
            )
            * self.view_span
        )

        self.view_start = max(
            0.0,
            min(
                1.0 - self.view_span,
                self.view_start - delta_normalized,
            ),
        )
        self._manual_hold_until = time.perf_counter() + 1.2
        self.view_changed.emit(
            self.view_start,
            self.view_span,
        )
        self.update()

    def marker_position_by_id(
        self,
        cue_id: int,
    ) -> float | None:
        for (
            existing_id,
            position,
            _label,
            _color,
            _locked,
            _marker_shape,
        ) in self.markers:
            if existing_id == cue_id:
                return float(position)
        return None

    def pointer_moved_past_drag_threshold(
        self,
        x: float,
        y: float,
    ) -> bool:
        distance = math.hypot(
            float(x) - self._press_mouse_x,
            float(y) - self._press_mouse_y,
        )
        return distance >= max(
            4,
            QApplication.startDragDistance(),
        )

    def mousePressEvent(self, event) -> None:
        if not self.wave_rect().contains(event.position()):
            super().mousePressEvent(event)
            return

        self.setFocus()
        modifiers = event.modifiers()
        x = float(event.position().x())
        y = float(event.position().y())

        self._press_mouse_x = x
        self._press_mouse_y = y
        self._marker_drag_started = False

        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton
            and modifiers & Qt.KeyboardModifier.ShiftModifier
        ):
            self._panning = True
            self._last_mouse_x = x
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            marker_id = self.marker_handle_at_point(
                x,
                y,
            )

            if marker_id is not None:
                if self.marker_is_locked(marker_id):
                    self._dragging_marker_id = None
                    self._scrubbing = False
                    self.setCursor(Qt.CursorShape.ForbiddenCursor)
                    event.accept()
                    return

                self._dragging_marker_id = marker_id
                marker_position = self.marker_position_by_id(marker_id)
                self._dragging_marker_position = (
                    marker_position if marker_position is not None else self.position_from_x(x)
                )
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                event.accept()
                return

            if not self.scrub_enabled:
                # An external clock drives the playhead; it is not ours to
                # move. Zoom and pan stay available.
                self._scrubbing = False
                self.setCursor(Qt.CursorShape.ForbiddenCursor)
                event.accept()
                return

            # Every other point on the waveform controls the playhead,
            # including positions directly over the marker's vertical line.
            self._scrubbing = True
            self._dragging_marker_id = None
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self._apply_scrub_position(self.position_from_x(x))
            self._request_scrub_seek(force=True)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        x = float(event.position().x())
        y = float(event.position().y())

        if self._dragging_marker_id is not None:
            if self._marker_drag_started or self.pointer_moved_past_drag_threshold(
                x,
                y,
            ):
                self._marker_drag_started = True
                self._dragging_marker_position = self.position_from_x(x)
                self.update_local_marker_position(
                    self._dragging_marker_id,
                    self._dragging_marker_position,
                )
            event.accept()
            return

        if self._scrubbing:
            # Press-and-hold anywhere on the waveform continuously moves
            # the playhead, making trackpad operation easy without a mouse.
            self._apply_scrub_position(self.position_from_x(x))
            self._request_scrub_seek()
            event.accept()
            return

        if self._panning and self.view_span < 1.0:
            delta_pixels = x - self._last_mouse_x
            self._last_mouse_x = x
            self.pan_view_by_pixels(delta_pixels)
            event.accept()
            return

        marker_id = self.marker_handle_at_point(
            x,
            y,
        )
        if marker_id is not None:
            if self.marker_is_locked(marker_id):
                self.setCursor(Qt.CursorShape.ForbiddenCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._dragging_marker_id is not None:
                cue_id = self._dragging_marker_id
                position = self._dragging_marker_position

                if self._marker_drag_started:
                    self.marker_move_finished.emit(
                        cue_id,
                        position,
                    )
                # A click on the marker handle alone intentionally does nothing.
                # It no longer moves the playhead to the marker.

            if self._scrubbing:
                self._end_scrub()

            self._dragging_marker_id = None
            self._marker_drag_started = False
            self._scrubbing = False
            self._panning = False
            self.setCursor(Qt.CursorShape.CrossCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            self.setCursor(Qt.CursorShape.CrossCursor)
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        rect = self.wave_rect()
        if not rect.contains(event.position()):
            super().wheelEvent(event)
            return

        pixel_delta = event.pixelDelta()
        angle_delta = event.angleDelta()

        using_pixel_delta = not (pixel_delta.x() == 0 and pixel_delta.y() == 0)

        if using_pixel_delta:
            delta_x = float(pixel_delta.x())
            delta_y = float(pixel_delta.y())
        else:
            # One wheel notch is 120 angle units.
            # Convert it to a moderate virtual pixel distance.
            delta_x = float(angle_delta.x()) / 8.0
            delta_y = float(angle_delta.y()) / 8.0

        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and abs(delta_x) < abs(
            delta_y
        ):
            delta_x = delta_y
            delta_y = 0.0

        horizontal_gesture = abs(delta_x) > abs(delta_y)

        if horizontal_gesture:
            # Two-finger horizontal movement pans the visible timeline.
            self.pan_view_by_pixels(delta_x)
            event.accept()
            return

        if abs(delta_y) > 0.0:
            cursor_ratio = (event.position().x() - rect.left()) / max(
                1.0,
                rect.width(),
            )

            if using_pixel_delta:
                factor = math.exp(-delta_y * 0.006)
                factor = max(
                    0.55,
                    min(
                        1.80,
                        factor,
                    ),
                )
            else:
                factor = 0.80 if delta_y > 0.0 else 1.25

            # Two-finger vertical movement zooms around the pointer.
            self.zoom_at(
                cursor_ratio,
                factor,
            )
            event.accept()
            return

        super().wheelEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self.wave_rect().contains(event.position()):
            self.fit_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def cached_wave_lines(
        self,
        wave_rect: QRectF,
    ) -> list[QLineF]:
        cache_key = (
            self._samples_revision,
            len(self.samples),
            round(self.view_start, 8),
            round(self.view_span, 8),
            round(wave_rect.width()),
            round(wave_rect.height()),
        )

        if cache_key == self._wave_lines_cache_key:
            return self._wave_lines_cache

        self._wave_lines_cache_key = cache_key
        self._wave_lines_cache = []

        if len(self.samples) <= 1:
            return self._wave_lines_cache

        total = len(self.samples)
        start_index = int(self.view_start * (total - 1))
        end_index = max(
            start_index + 2,
            int((self.view_start + self.view_span) * (total - 1)) + 1,
        )
        end_index = min(total, end_index)
        visible = self.samples[start_index:end_index]

        if len(visible) <= 0:
            return self._wave_lines_cache

        count = min(
            max(
                1,
                int(wave_rect.width() / 2),
            ),
            len(visible),
        )
        indices = np.linspace(
            0,
            len(visible) - 1,
            count,
        ).astype(int)
        values = np.clip(
            visible[indices],
            0.0,
            1.0,
        )

        center_y = wave_rect.center().y()
        amplitude = wave_rect.height() * 0.46
        step = wave_rect.width() / max(1, count - 1)

        self._wave_lines_cache = [
            QLineF(
                wave_rect.left() + index * step,
                center_y
                - max(
                    2.5,
                    float(value) * amplitude,
                ),
                wave_rect.left() + index * step,
                center_y
                + max(
                    2.5,
                    float(value) * amplitude,
                ),
            )
            for index, value in enumerate(values)
        ]
        return self._wave_lines_cache

    def _reset_waveform_tile_cache(self) -> None:
        self._wave_tile_cache.clear()
        self._wave_pixmap_cache_key = None
        self._wave_pixmap_cache = QPixmap()

    def _rebuild_waveform_pyramid(self) -> None:
        if len(self.samples) <= 1:
            base = np.zeros(
                (max(1, len(self.samples)), 1),
                dtype=np.float32,
            )
        else:
            base = np.ascontiguousarray(
                self.samples.reshape(-1, 1),
                dtype=np.float32,
            )

        try:
            self._wave_pyramid = build_waveform_pyramid(base)
        except Exception:
            self._wave_pyramid = (np.ascontiguousarray(base),)

        self._reset_waveform_tile_cache()
        self._last_lod_level = 0

    def _waveform_lod_level(self, wave_rect: QRectF) -> int:
        if not self._wave_pyramid:
            return 0
        level = select_lod_level(
            self._wave_pyramid,
            self.view_span,
            wave_rect.width(),
            target_points_per_pixel=1.35,
        )
        self._last_lod_level = int(level)
        return int(level)

    def _render_waveform_tile(
        self,
        level_index: int,
        tile_index: int,
        logical_height: int,
    ) -> QPixmap:
        if not self._wave_pyramid:
            return QPixmap()

        level_index = max(0, min(len(self._wave_pyramid) - 1, int(level_index)))
        data = self._wave_pyramid[level_index]
        start = int(tile_index) * self._wave_tile_size
        end = min(len(data), start + self._wave_tile_size)
        if start >= end:
            return QPixmap()

        cache_key = (
            self._samples_revision,
            level_index,
            int(tile_index),
            int(logical_height),
        )
        cached = self._wave_tile_cache.get(cache_key)
        if cached is not None and not cached.isNull():
            self._wave_tile_cache.move_to_end(cache_key)
            return cached

        count = end - start
        pixmap = QPixmap(max(1, count), max(1, int(logical_height)))
        pixmap.fill(Qt.GlobalColor.transparent)

        center_y = logical_height * 0.5
        amplitude_pixels = logical_height * 0.46
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        painter.setPen(
            QPen(
                QColor("#70756F"),
                1.0,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.FlatCap,
            )
        )
        tile = data[start:end]
        for local_index, row in enumerate(tile):
            amplitude = float(row[0])
            height = max(2.0, amplitude * amplitude_pixels)
            x = float(local_index) + 0.5
            painter.drawLine(
                QLineF(
                    x,
                    center_y - height,
                    x,
                    center_y + height,
                )
            )

        painter.end()
        self._wave_tile_cache[cache_key] = pixmap
        self._wave_tile_cache.move_to_end(cache_key)
        while len(self._wave_tile_cache) > self._wave_tile_cache_limit:
            self._wave_tile_cache.popitem(last=False)
        return pixmap

    def _draw_waveform_tiles(
        self,
        painter: QPainter,
        wave_rect: QRectF,
    ) -> None:
        if not self._wave_pyramid or wave_rect.width() <= 1.0:
            return

        level_index = self._waveform_lod_level(wave_rect)
        level = self._wave_pyramid[level_index]
        count = len(level)
        if count <= 0:
            return

        logical_height = max(1, round(wave_rect.height()))
        visible_start = max(0.0, min(float(count), self.view_start * count))
        visible_end = max(
            visible_start + 1.0,
            min(float(count), (self.view_start + self.view_span) * count),
        )

        first_tile = max(0, math.floor(visible_start / self._wave_tile_size))
        last_tile = min(
            max(0, (count - 1) // self._wave_tile_size),
            math.floor((visible_end - 1.0e-9) / self._wave_tile_size),
        )

        painter.save()
        painter.setClipRect(wave_rect)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)

        for tile_index in range(first_tile, last_tile + 1):
            start = tile_index * self._wave_tile_size
            end = min(count, start + self._wave_tile_size)
            if end <= start:
                continue

            pixmap = self._render_waveform_tile(
                level_index,
                tile_index,
                logical_height,
            )
            if pixmap.isNull():
                continue

            norm_start = start / float(count)
            norm_end = end / float(count)
            dest_left = (
                wave_rect.left()
                + ((norm_start - self.view_start) / max(self.view_span, 1.0e-9))
                * wave_rect.width()
            )
            dest_right = (
                wave_rect.left()
                + ((norm_end - self.view_start) / max(self.view_span, 1.0e-9))
                * wave_rect.width()
            )
            dest = QRectF(
                dest_left,
                wave_rect.top(),
                max(0.5, dest_right - dest_left),
                wave_rect.height(),
            )
            painter.drawPixmap(
                dest,
                pixmap,
                QRectF(0.0, 0.0, float(pixmap.width()), float(pixmap.height())),
            )

        painter.restore()

    def draw_marker_handle(
        self,
        painter: QPainter,
        handle_rect: QRectF,
        marker_color: QColor,
        marker_shape: str,
        marker_locked: bool,
    ) -> None:
        fill_color = marker_color if not marker_locked else marker_color.darker(135)
        shape = normalize_marker_shape(marker_shape)

        # Every shape is drawn inside the same 10 x 10 square.
        # The bottom edge is shared so all shapes connect evenly
        # to the vertical marker line.
        visual_size = min(
            float(self._marker_visual_size),
            handle_rect.width(),
            handle_rect.height(),
        )
        visual_rect = QRectF(
            handle_rect.center().x() - visual_size / 2.0,
            handle_rect.bottom() - visual_size,
            visual_size,
            visual_size,
        )

        shape_rect = visual_rect.adjusted(
            0.7,
            0.7,
            -0.7,
            -0.7,
        )

        painter.save()
        painter.setRenderHint(
            QPainter.RenderHint.Antialiasing,
            True,
        )
        painter.setPen(
            QPen(
                marker_color.lighter(112),
                1.0,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(fill_color)

        if shape == "circle":
            painter.drawEllipse(shape_rect)
            painter.restore()
            return

        if shape == "triangle":
            path = QPainterPath()
            path.moveTo(
                shape_rect.center().x(),
                shape_rect.bottom(),
            )
            path.lineTo(
                shape_rect.left(),
                shape_rect.top(),
            )
            path.lineTo(
                shape_rect.right(),
                shape_rect.top(),
            )
            path.closeSubpath()
            painter.drawPath(path)
            painter.restore()
            return

        if shape == "diamond":
            path = QPainterPath()
            path.moveTo(
                shape_rect.center().x(),
                shape_rect.top(),
            )
            path.lineTo(
                shape_rect.right(),
                shape_rect.center().y(),
            )
            path.lineTo(
                shape_rect.center().x(),
                shape_rect.bottom(),
            )
            path.lineTo(
                shape_rect.left(),
                shape_rect.center().y(),
            )
            path.closeSubpath()
            painter.drawPath(path)
            painter.restore()
            return

        if shape == "heart":
            x = shape_rect.left()
            y = shape_rect.top()
            width = shape_rect.width()
            height = shape_rect.height()

            path = QPainterPath()
            path.moveTo(
                x + width * 0.50,
                y + height,
            )
            path.cubicTo(
                x + width * 0.16,
                y + height * 0.72,
                x,
                y + height * 0.48,
                x,
                y + height * 0.29,
            )
            path.cubicTo(
                x,
                y + height * 0.04,
                x + width * 0.31,
                y - height * 0.01,
                x + width * 0.50,
                y + height * 0.23,
            )
            path.cubicTo(
                x + width * 0.69,
                y - height * 0.01,
                x + width,
                y + height * 0.04,
                x + width,
                y + height * 0.29,
            )
            path.cubicTo(
                x + width,
                y + height * 0.48,
                x + width * 0.84,
                y + height * 0.72,
                x + width * 0.50,
                y + height,
            )
            path.closeSubpath()
            painter.drawPath(path)
            painter.restore()
            return

        painter.drawRoundedRect(
            shape_rect,
            1.8,
            1.8,
        )
        painter.restore()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setClipRegion(event.region())
        except Exception:
            pass
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(
            self.rect(),
            QColor("#070A05"),
        )

        painter.setPen(QColor("#ffffff"))
        title_rect = self.rect().adjusted(
            18,
            8,
            -18,
            -self.height() + 38,
        )
        fm = QFontMetrics(painter.font())
        zoom_text = f"{1.0 / self.view_span:.0f}×"
        available = max(
            20,
            title_rect.width() - fm.horizontalAdvance(zoom_text) - 20,
        )
        title = fm.elidedText(
            self.title,
            Qt.TextElideMode.ElideRight,
            available,
        )
        painter.drawText(
            title_rect,
            (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            title,
        )
        painter.drawText(
            title_rect,
            (Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            zoom_text,
        )

        if self._gpu_surface is not None:
            self._layout_gpu_children()
            self._sync_gpu_static()
            self._sync_gpu_progress()
            return

        wave_rect = self.wave_rect()
        playhead_color = QColor("#D9FFE1")

        visible_progress = (self.progress - self.view_start) / max(
            self.view_span,
            1e-9,
        )
        progress_x = wave_rect.left() + wave_rect.width() * visible_progress

        # DJ-style render: draw only the currently visible gray LOD tiles once at
        # full brightness. The played section is dimmed with a cheap translucent
        # overlay, so playhead movement never rebuilds waveform data.
        self._draw_waveform_tiles(painter, wave_rect)

        clipped_progress = max(0.0, min(1.0, visible_progress))
        if clipped_progress > 0.0:
            played_width = wave_rect.width() * clipped_progress
            painter.fillRect(
                QRectF(
                    wave_rect.left(),
                    wave_rect.top(),
                    max(0.0, played_width),
                    wave_rect.height(),
                ),
                QColor(0, 0, 0, 178),
            )

        if 0.0 <= visible_progress <= 1.0:
            painter.setPen(
                QPen(
                    playhead_color,
                    2.2,
                )
            )
            painter.drawLine(
                QLineF(
                    progress_x,
                    wave_rect.top(),
                    progress_x,
                    wave_rect.bottom(),
                )
            )

        marker_start = bisect_left(
            self._marker_positions,
            self.view_start,
        )
        marker_end = bisect_right(
            self._marker_positions,
            self.view_start + self.view_span,
        )

        for (
            _cue_id,
            marker_position,
            _marker_label,
            marker_hex,
            marker_locked,
            marker_shape,
        ) in self.markers[marker_start:marker_end]:
            visible_marker = (marker_position - self.view_start) / max(
                self.view_span,
                1e-9,
            )
            marker_x = wave_rect.left() + wave_rect.width() * visible_marker
            marker_color = QColor(marker_hex)
            if not marker_color.isValid():
                marker_color = QColor("#ffb020")

            painter.setPen(
                QPen(
                    marker_color,
                    (3.2 if marker_locked else 2.2),
                )
            )
            painter.drawLine(
                QLineF(
                    marker_x,
                    (wave_rect.top() + self._marker_handle_height),
                    marker_x,
                    wave_rect.bottom(),
                )
            )

            handle_rect = QRectF(
                (marker_x - self._marker_handle_width / 2.0),
                wave_rect.top(),
                self._marker_handle_width,
                self._marker_handle_height,
            )
            self.draw_marker_handle(
                painter,
                handle_rect,
                marker_color,
                marker_shape,
                marker_locked,
            )

        self._paint_peer_presence(painter, wave_rect)
