from __future__ import annotations

import threading
import uuid
from pathlib import Path

import numpy as np
from PySide6.QtCore import (
    QEvent,
    QLineF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedLayout,
    QStackedWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from tracyy.audio import Transport
from tracyy.controllers import (
    AudioOutputControllerMixin,
    CueControllerMixin,
    CueFollowControllerMixin,
    LicenseControllerMixin,
    LTCControllerMixin,
    MediaRelinkControllerMixin,
    PlaylistControllerMixin,
    ProjectControllerMixin,
    ServerControllerMixin,
    SyncControllerMixin,
)
from tracyy.core import (
    AdaptiveWrapLabel,
    RateLimiter,
    Tier,
    UiTickScheduler,
    get_logger,
    silent_message_box,
    tracyy_resource_root,
)
from tracyy.core.ids import new_origin
from tracyy.licensing.client import (
    LicenseClient,
)
from tracyy.ltc.input import (
    LTCAudioInput,
    LTCFrameContinuityValidator,
)
from tracyy.modules import LicensePolicy, TracyyModuleRegistry
from tracyy.sync.client import LiveClient
from tracyy.sync.server import LiveServer
from tracyy.sync.session import SyncSession
from tracyy.ui.surfaces import RaisedIconButton
from tracyy.widgets import (
    CueTableDelegate,
    CueTablePresenceOverlay,
    PlaylistDropListWidget,
    VirtualCueLoadingWidget,
    WaveformWidget,
)
from tracyy.workers import MediaWorkerPool

# DJ waveform trial: use Qt's OpenGL surface when available. Set


_log = get_logger(__name__)


class OverlayStackWidget(QWidget):
    """Content at full height with one widget floating over its bottom edge.

    A QVBoxLayout hands the overlay its own strip and takes that height away
    from the content above it, so the cue list stopped dead where the transport
    began. Here the list gets the whole area and the transport is laid over the
    bottom of it, which is what a player looks like: rows keep going and slide
    out of sight under the controls instead of hitting a wall.
    """

    #: Gap between the floating overlay and the edges of this widget. Without
    #: it the lower corners round straight into the window edge and the
    #: rounding is invisible — a card has to have somewhere to float.
    OVERLAY_MARGIN = 10

    def __init__(self, content: QWidget, overlay: QWidget, parent=None) -> None:
        super().__init__(parent)
        self._content = content
        self._overlay = overlay
        content.setParent(self)
        overlay.setParent(self)
        overlay.raise_()

    def overlay_height(self) -> int:
        hint = self._overlay.sizeHint().height()
        return max(hint, self._overlay.minimumSizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._content.setGeometry(0, 0, self.width(), self.height())

        margin = self.OVERLAY_MARGIN
        height = self.overlay_height()
        self._overlay.setGeometry(
            margin,
            self.height() - height - margin,
            max(0, self.width() - 2 * margin),
            height,
        )

        setter = getattr(self._content, "set_bottom_inset", None)
        if callable(setter):
            setter(height + margin)


class TracyyApplication(QApplication):
    project_file_requested = Signal(str)

    def __init__(self, argv) -> None:
        super().__init__(argv)
        self._pending_project_files: list[str] = []

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.FileOpen:
            try:
                path = str(event.file()).strip()
            except Exception:
                path = ""
            if path:
                self._pending_project_files.append(path)
                self.project_file_requested.emit(path)
            return True
        return super().event(event)

    def pending_project_files(self) -> list[str]:
        files = list(self._pending_project_files)
        self._pending_project_files.clear()
        return files


class MainWindow(
    SyncControllerMixin,
    ServerControllerMixin,
    MediaRelinkControllerMixin,
    PlaylistControllerMixin,
    CueControllerMixin,
    ProjectControllerMixin,
    CueFollowControllerMixin,
    AudioOutputControllerMixin,
    LTCControllerMixin,
    LicenseControllerMixin,
    QMainWindow,
):
    license_connectivity_checked = Signal(bool)
    usb_license_scan_finished = Signal(
        object,
        str,
    )
    google_sheet_scan_finished = Signal(
        object,
        str,
    )
    main_preview_ready = Signal(str, object, int)
    standby_warmup_ready = Signal(int, str, bool)
    standby_progressive_update = Signal(int, str, int, int, bool)
    standby_preview_ready = Signal(str, object, int, int)
    preview_failed = Signal(str, str)
    playlist_preload_ready = Signal(str, object, bool, str)
    playlist_preload_progress = Signal(str, int)
    input_timecode_frame_received = Signal(object)
    input_timecode_runtime_status = Signal(str)

    def navigation_icon(
        self,
        icon_name: str,
    ) -> QIcon:
        key = str(icon_name or "").strip().lower()

        cache = getattr(
            self,
            "_navigation_icon_cache",
            None,
        )
        if not isinstance(cache, dict):
            cache = {}
            self._navigation_icon_cache = cache

        cached = cache.get(key)
        if isinstance(cached, QIcon):
            return cached

        # Drawn at the display's pixel ratio, not at 22 flat: a 22x22 pixmap
        # upscaled on a Retina panel is the soft, slightly muddy look these
        # icons had. Painting is in logical units — Qt applies the ratio to the
        # pixmap itself, so scaling here as well would double it.
        try:
            ratio = float(self.devicePixelRatioF())
        except Exception:
            ratio = 2.0
        ratio = max(1.0, ratio)
        pixmap = QPixmap(round(22 * ratio), round(22 * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(
            QPainter.RenderHint.Antialiasing,
            True,
        )

        # One tone per icon. Two colours inside a 22px glyph reads as clip art;
        # the selected state is carried by the button behind it, which is where
        # state belongs. Same tone as the transport glyphs, so the whole chrome
        # is one family.
        foreground = QColor("#C8CCD4")
        accent = foreground
        painter.setPen(
            QPen(
                foreground,
                1.8,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if key == "file":
            page = QPainterPath()
            page.moveTo(5.0, 3.0)
            page.lineTo(13.0, 3.0)
            page.lineTo(17.0, 7.0)
            page.lineTo(17.0, 19.0)
            page.lineTo(5.0, 19.0)
            page.closeSubpath()
            painter.drawPath(page)
            painter.drawLine(QLineF(13.0, 3.2, 13.0, 7.2))
            painter.drawLine(QLineF(13.0, 7.0, 16.8, 7.0))
            painter.setPen(
                QPen(
                    accent,
                    1.6,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            painter.drawLine(QLineF(8.0, 11.0, 14.0, 11.0))
            painter.drawLine(QLineF(8.0, 14.5, 14.0, 14.5))

        elif key == "settings":
            # Sliders, not a cog. A cog needs its teeth to read as a cog, and
            # at 22px there is no room for teeth — the eight spokes this drew
            # landed as a sun. Three tracks with handles stay legible at any
            # size and say "adjust" more directly anyway.
            for track_y, knob_x in ((6.0, 14.0), (11.0, 8.0), (16.0, 15.0)):
                painter.drawLine(QLineF(3.5, track_y, 18.5, track_y))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(foreground)
                painter.drawEllipse(QRectF(knob_x - 2.0, track_y - 2.0, 4.0, 4.0))
                painter.setPen(
                    QPen(
                        foreground,
                        1.8,
                        Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin,
                    )
                )
                painter.setBrush(Qt.BrushStyle.NoBrush)

        elif key == "sync":
            # Three linked nodes. The Server icon is already a stack of boxes
            # (one machine), so this one has to say "several machines, joined"
            # — and at 22px a link between nodes reads where two overlapping
            # figures would just smudge.
            nodes = ((5.6, 6.4), (16.4, 6.4), (11.0, 16.6))
            painter.drawLine(QLineF(nodes[0][0], nodes[0][1], nodes[1][0], nodes[1][1]))
            painter.drawLine(QLineF(nodes[0][0], nodes[0][1], nodes[2][0], nodes[2][1]))
            painter.drawLine(QLineF(nodes[1][0], nodes[1][1], nodes[2][0], nodes[2][1]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#0F1310"))
            for x, y in nodes:
                painter.drawEllipse(QRectF(x - 3.1, y - 3.1, 6.2, 6.2))
            painter.setPen(
                QPen(
                    foreground,
                    1.8,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.setBrush(foreground)
            for x, y in nodes:
                painter.drawEllipse(QRectF(x - 2.3, y - 2.3, 4.6, 4.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)

        elif key == "server":
            for top in (3.0, 9.0, 15.0):
                painter.drawRoundedRect(
                    QRectF(3.0, top, 16.0, 4.0),
                    1.2,
                    1.2,
                )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            for center_y in (5.0, 11.0, 17.0):
                painter.drawEllipse(QRectF(5.0, center_y - 0.9, 1.8, 1.8))
            painter.setBrush(foreground)
            for center_y in (5.0, 11.0, 17.0):
                painter.drawRoundedRect(
                    QRectF(9.0, center_y - 0.6, 7.0, 1.2),
                    0.6,
                    0.6,
                )

        elif key == "sync":
            path = QPainterPath()
            path.moveTo(2.5, 11.0)
            path.lineTo(6.0, 11.0)
            path.lineTo(8.0, 5.0)
            path.lineTo(11.0, 17.0)
            path.lineTo(14.0, 8.0)
            path.lineTo(16.0, 11.0)
            path.lineTo(19.5, 11.0)
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(QRectF(8.8, 8.8, 4.4, 4.4))

        painter.end()

        icon = QIcon(pixmap)
        cache[key] = icon
        return icon

    def brand_logo_path(self) -> Path:
        icon_dir = tracyy_resource_root() / "icon"
        for filename in (
            "logo.png",
            "Tracyy.png",
            "Tracyy.ico",
        ):
            candidate = icon_dir / filename
            if candidate.exists():
                return candidate
        return icon_dir / "logo.png"

    def load_brand_logo(self) -> QPixmap:
        path = self.brand_logo_path()
        if not path.exists():
            return QPixmap()
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            fallback = tracyy_resource_root() / "icon" / "Tracyy.png"
            if fallback.exists():
                pixmap = QPixmap(str(fallback))
        return pixmap

    def __init__(self) -> None:
        super().__init__()

        # Check license exactly once during application startup.
        # When offline, LicenseClient uses the last saved status.
        startup_license_client = LicenseClient()
        self.license_result = startup_license_client.check()
        self.license_client = startup_license_client
        self.license_policy = LicensePolicy(self.license_result.status)
        self.modules = TracyyModuleRegistry()

        # License status is obtained only once during application startup.
        self._startup_license_status = str(self.license_result.status).strip().upper()
        self._startup_license_message = str(self.license_result.message or "")
        self._license_blocked_by_offline_timeout = False

        self._license_expiry_timer = QTimer(self)
        self._license_expiry_timer.setSingleShot(True)
        self._license_expiry_timer.timeout.connect(self.enforce_local_license_deadline)

        self._usb_runtime_state = startup_license_client.usb_runtime_state
        self._usb_admin_timer = QTimer(self)
        self._usb_admin_timer.setInterval(1000)
        self._usb_admin_timer.timeout.connect(self.update_usb_admin_runtime)

        self._usb_license_scan_in_progress = False
        self._google_sheet_scan_in_progress = False
        self.usb_license_scan_finished.connect(self.apply_manual_usb_license_scan_result)
        self.google_sheet_scan_finished.connect(self.apply_manual_google_sheet_scan_result)

        # Update license countdown text and local expiry once per second,
        # even when the License Information dialog is closed.
        self._license_display_timer = QTimer(self)
        self._license_display_timer.setInterval(1000)
        self._license_display_timer.timeout.connect(self.refresh_license_countdown_display)

        # Runtime connectivity monitor only. It does not request
        # ACTIVE/BLOCKED/REVOKED status from the license server.
        self._license_connectivity_interval_ms = 3_000
        self._license_connectivity_check_in_progress = False
        self._license_connectivity_timer = QTimer(self)
        self._license_connectivity_timer.setInterval(self._license_connectivity_interval_ms)
        self._license_connectivity_timer.timeout.connect(
            self.request_license_connectivity_check
        )
        self.license_connectivity_checked.connect(self.apply_license_connectivity)
        # Cue tracking remains available for all states. Only REVOKED
        # removes the visual countdown according to the license matrix.
        self._cue_runtime_enabled = True
        self._cue_countdown_enabled = self.license_policy.access.cue_countdown
        self._current_project_path: str | None = None
        self._saved_project_signature = ""
        self._project_dirty_revision = 0
        self._shutdown_in_progress = False

        self._project_backup_timer = QTimer(self)
        self._project_backup_timer.setSingleShot(False)
        self._project_backup_timer.setInterval(15 * 60 * 1000)
        self._project_backup_timer.timeout.connect(self.write_project_backup)

        self._standby_preload_timer = QTimer(self)
        self._standby_preload_timer.setSingleShot(True)
        self._standby_preload_timer.setInterval(650)
        self._standby_preload_timer.timeout.connect(self._perform_standby_preload)
        self._standby_preload_row = -1
        self._standby_preload_path = ""
        self._standby_preloaded_path = ""
        self._standby_warmup_generation = 0
        self._standby_progressive_generation = 0
        self._standby_progressive_cancel = threading.Event()
        self._standby_progressive_path = ""
        self._standby_progressive_loaded = 0
        self._standby_progressive_total = 0
        self._standby_ready_threshold = 0
        self.standby_progressive_update.connect(self._on_standby_progressive_update)
        self.standby_warmup_ready.connect(self._on_standby_warmup_ready)

        self.setWindowTitle("Tracyy — Licensed Prototype")

        brand_logo = self.load_brand_logo()
        if not brand_logo.isNull():
            self.setWindowIcon(QIcon(brand_logo))

        self.resize(1280, 820)

        self.transport = Transport()
        # V1.2.3: playback remains bounded streaming; waveform is full-track peak analysis in spawned workers.
        # On an 8-core Mac this defaults to four workers, leaving headroom for
        # CoreAudio, Qt and macOS itself.
        self.media_workers = MediaWorkerPool()
        self.transport.status_changed.connect(self.set_status)
        self.transport.error.connect(self.show_error)
        self.transport.finished.connect(self.on_track_finished)
        self.transport.buffer_ready.connect(self.on_transport_buffer_ready)
        self.transport.buffer_state_changed.connect(self.on_transport_buffer_state)

        self._seeking = False
        self.cues_by_track: dict[str, list[dict[str, object]]] = {}
        # Local path -> track id. The cue table stays keyed by path so the
        # hundred call sites that look a track up by where it is on this disk
        # keep working; the id is what leaves the machine, in the project file
        # and on the wire, because the same song sits at a different path on
        # every laptop and under a different separator on Windows.
        self.track_ids: dict[str, str] = {}
        # Cue buckets whose song is not on this machine. Keyed by track id
        # rather than by path, and written back out under it untouched.
        self._orphan_cue_track_ids: set[str] = set()
        self._undo_stack: list[dict[str, object]] = []
        self._undo_limit = 40
        self._restoring_undo = False
        self._updating_cue_table = False
        self._cue_table_load_token = 0
        self._cue_table_batch_size = 80
        self._cue_table_initial_batch_size = 120
        self._cue_table_batch_interval_ms = 4
        self._cue_table_pending_cues: list[dict[str, object]] = []
        self._cue_table_pending_index = 0
        self._cue_table_pending_path: str | None = None
        self._cue_table_pending_selected_ids: list[int] = []
        self._cue_table_pending_current_id: int | None = None
        self._active_playback_cue_id: int | None = None
        self._last_playhead_position = -1.0

        # Cue Table runtime indexes. These are rebuilt only when cue data
        # changes, not on every playback timer tick.
        self._cue_sorted_cache: list[dict[str, object]] = []
        self._cue_times_cache: list[float] = []
        self._cue_id_to_row: dict[int, int] = {}
        self._cue_id_to_cue: dict[int, dict[str, object]] = {}
        self._next_cue_index = 0
        self._cue_runtime_rows: set[int] = set()
        self._timeline_loading_last_update = 0.0
        self._timeline_loading_window_key: tuple | None = None
        self._cue_manifest_revision = 0
        self._cue_table_mouse_inside = False
        self._cue_table_manual_browse = False
        self._cue_table_leave_token = 0
        self._cue_table_label_hover = False
        self._cue_table_label_editor_active = False
        self._cue_table_label_editor_cue_id: int | None = None
        self.playing_playlist_row = -1
        self.standby_playlist_row = -1
        self.standby_path: str | None = None
        self.standby_preview_cache: dict[str, np.ndarray] = {}
        self._playlist_preload_queue: list[str] = []
        self._playlist_preload_pending: set[str] = set()
        self._playlist_preload_running = False
        self._playlist_preload_cancelled = False
        self._playlist_preload_lock = threading.RLock()
        self._playlist_preload_state: dict[str, str] = {}
        self._ram_ready_paths: set[str] = set()
        self._pending_play_after_ram_path = ""
        self._pending_play_after_ram_position = 0.0
        self._pending_activate_after_ram_path = ""
        self._pending_activate_after_ram_autoplay = False
        self._main_preview_token = 0
        self._standby_preview_token = 0
        self.track_offsets: dict[str, int] = {}
        self._input_timecode_engine: LTCAudioInput | None = None
        self._input_timecode_enabled = False
        self._input_timecode_locked = False
        self._input_timecode_signal_state = "OFF"
        self._input_timecode_last_received_at = 0.0
        self._input_timecode_last_raw_received_at = 0.0
        self._input_timecode_anchor_seconds = 0.0
        self._input_timecode_anchor_frames = 0
        self._input_timecode_input_fps = 30
        self._input_timecode_delivery_latency = 0.0
        self._input_timecode_phase_frames = 1
        self._input_timecode_project_fps = 30
        self._input_timecode_active_path = ""
        self._input_timecode_manual_preview = False
        self._input_timecode_match_paths: list[str] = []
        self._input_timecode_match_signature: tuple[str, ...] = ()
        self._input_timecode_duration_cache: dict[str, float] = {}
        self._input_timecode_pre_roll_default = 0.50
        self._input_timecode_after_roll_default = 0.20
        self._input_timecode_fps_default = 30
        self._input_timecode_latency_ms_default = 0.0
        self._input_timecode_lost_timeout = 0.65
        self._input_timecode_last_status = ""

        self._input_timecode_validator = LTCFrameContinuityValidator(
            fps=self._input_timecode_input_fps,
            pre_roll_seconds=self._input_timecode_pre_roll_default,
            forward_dropout_tolerance=4,
            backward_jitter_guard=3,
            seek_confirm_frames=4,
            zero_confirm_frames=5,
        )

        # Owns "are we in a session, who else is here, what may I do". The
        # network layer will feed it; nothing below reaches past it.
        self.sync_session = SyncSession(self)
        # The two ends of the wire. Both are Qt-free and are read from this
        # thread on the tick below; neither ever calls back into the window.
        self.live_server = LiveServer()
        self.live_client = LiveClient()
        self._sync_peer_signature: tuple = ()

        # Prefix for every cue and track id this run mints. The machine part
        # makes a run of ids recognisable as one laptop's work; the random
        # part is what keeps them unique, because the counter behind them
        # restarts with the process and would otherwise hand out a number this
        # machine had already used yesterday.
        self.id_origin = f"{self.sync_session.machine_id[:4]}{new_origin(4)}"

        self._server_state_lock = threading.RLock()
        self._server_state: dict[str, object] = {
            "timecode": "00:00:00:00",
            "state": "STOPPED",
            "offset": "00:00:00:00",
            "elapsed": "00:00:00",
            "duration": "00:00:00",
            "track": "No track selected",
            "cues": [],
        }
        self.server_profiles: list[dict[str, object]] = []
        self._next_server_profile_id = 1
        self._last_server_snapshot: dict[str, object] = {}
        self._server_manifest_cache_key: tuple | None = None
        self._server_manifest_cache: list[dict[str, object]] = []
        self._server_catalog_cache_key: tuple | None = None
        self._server_catalog_cache: list[tuple[str, str]] = []
        self._server_snapshot_last_refresh = 0.0
        # Viewer-count labels by profile id. Held here rather than on the
        # profile dicts because rebuilding the cards destroys the old labels,
        # and a dict cleared on every rebuild cannot outlive them.
        self._server_viewer_labels: dict[int, QLabel] = {}
        self._server_viewers_last_read = 0.0
        self._server_session_id = uuid.uuid4().hex
        self._server_transport_sequence = 0
        self._server_transport_signature: tuple | None = None
        self._server_transport_anchor_position = 0.0
        self._server_transport_anchor_monotonic = 0.0
        self._server_transport_anchor_state = "STOPPED"

        self.main_preview_ready.connect(self._apply_main_preview)
        self.standby_preview_ready.connect(self._apply_standby_preview)
        self.preview_failed.connect(self._on_preview_failed)
        self.playlist_preload_ready.connect(self._on_playlist_preload_ready)
        self.playlist_preload_progress.connect(self._on_playlist_preload_progress)

        self.cue_type_defs = [
            ("Choreo", "#ff7a45"),
            ("Audio", "#2bb9ff"),
            ("Video", "#7a43ff"),
            ("Notes", "#3d74ff"),
            ("Lighting", "#e44343"),
            ("Pyro", "#1fd95d"),
            ("Automation", "#d63cff"),
        ]
        self.cue_type_meta: dict[str, dict[str, bool]] = {}
        self.ensure_cue_type_meta()
        self.active_cue_type = "Choreo"
        self._build_ui()
        self.restore_auto_backup_preference()
        self._setup_cue_keyboard_shortcuts()
        self.input_timecode_frame_received.connect(self.on_input_timecode_frame)
        self.input_timecode_runtime_status.connect(self.on_input_timecode_runtime_status)
        self._input_timecode_engine = LTCAudioInput(
            on_frame=lambda frame: self.input_timecode_frame_received.emit(frame),
            on_status=lambda message: self.input_timecode_runtime_status.emit(message),
        )
        self.refresh_devices()

        self._setup_tick_scheduler()

        # The initial empty project is the clean baseline.
        # Closing Tracyy immediately must not show a save prompt.
        self.mark_project_clean()

        # License status was checked once at startup. Monitor only
        # network reachability while the application remains open.
        self.refresh_license_countdown_display()
        QTimer.singleShot(
            500,
            self.request_license_connectivity_check,
        )

    def _setup_tick_scheduler(self) -> None:
        """One timer for every periodic UI job.

        V1 ran six: a 15 ms playhead timer plus four polling timers at one
        second and one at fifteen minutes, each an independent wakeup source
        that kept the CPU from idling. V2 registers the same work as tiers on
        :class:`~tracyy.core.perf.UiTickScheduler`, which paces them from a
        single timer, slows every tier down when the window is hidden, and
        backs off further if the machine cannot keep up.
        """
        self.tick_scheduler = UiTickScheduler(self)
        self.tick_scheduler.job_failed.connect(self._on_tick_job_failed)

        self.tick_scheduler.add(Tier.PLAYHEAD, self.tick_playhead, name="playhead")
        self.tick_scheduler.add(Tier.LABELS, self.tick_labels, name="labels")
        self.tick_scheduler.add(Tier.NETWORK, self.tick_network, name="server_snapshot")
        self.tick_scheduler.add(Tier.BACKGROUND, self.tick_sync, name="live_peers")
        self.tick_scheduler.add(
            Tier.BACKGROUND,
            self.update_usb_admin_runtime,
            name="usb_admin",
        )
        self.tick_scheduler.add(
            Tier.BACKGROUND,
            self.refresh_license_countdown_display,
            name="license_countdown",
        )
        self.tick_scheduler.add(
            Tier.BACKGROUND,
            self._tick_license_connectivity,
            name="license_connectivity",
        )
        self.tick_scheduler.add(Tier.BACKGROUND, self._tick_activity_state, name="activity")

        # The licence controller still switches these two on and off; the
        # handles answer to the QTimer methods it already calls.
        self._usb_admin_timer = self.tick_scheduler.handle("usb_admin")
        self._license_connectivity_timer = self.tick_scheduler.handle("license_connectivity")
        self._license_display_timer = self.tick_scheduler.handle("license_countdown")

        self.tick_scheduler.start()

    def _tick_activity_state(self) -> None:
        """Tell the scheduler how hard it should be working."""
        self.tick_scheduler.update_state(
            playing=bool(self.transport.running or self._input_timecode_locked),
            visible=bool(self.isVisible() and not self.isMinimized()),
        )

    def _tick_license_connectivity(self) -> None:
        """Throttle the connectivity probe down to its own long interval.

        It lives on the background tier so it shares the one timer, but a
        network probe every second is neither wanted nor polite.
        """
        limiter = getattr(self, "_license_connectivity_limiter", None)
        if limiter is None:
            limiter = self._license_connectivity_limiter = RateLimiter(
                max(1.0, self._license_connectivity_interval_ms / 1000.0)
            )
        if limiter.ready():
            self.request_license_connectivity_check()

    def _on_tick_job_failed(self, name: str, message: str) -> None:
        _log.warning("periodic UI job %r failed: %s", name, message)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        # Top brand header.  The logo belongs to the application header,
        # not to the compact navigation rail.
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        top_header = QWidget()
        top_header.setObjectName("topHeader")
        top_header.setFixedHeight(62)
        top_header_layout = QHBoxLayout(top_header)
        top_header_layout.setContentsMargins(18, 7, 18, 7)
        top_header_layout.setSpacing(11)

        self.brand_logo_label = QLabel()
        self.brand_logo_label.setObjectName("brandLogoHeader")
        self.brand_logo_label.setFixedSize(44, 44)
        self.brand_logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        brand_logo = self.load_brand_logo()
        if not brand_logo.isNull():
            self.brand_logo_label.setPixmap(
                brand_logo.scaled(
                    42,
                    42,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.brand_logo_label.hide()

        brand_text_box = QWidget()
        brand_text_layout = QVBoxLayout(brand_text_box)
        brand_text_layout.setContentsMargins(0, 0, 0, 0)
        brand_text_layout.setSpacing(0)

        nav_title = QLabel("TRACYY")
        nav_title.setObjectName("headerBrandTitle")
        nav_subtitle = QLabel("SHOW CONTROL")
        nav_subtitle.setObjectName("headerBrandSubtitle")
        brand_text_layout.addWidget(nav_title)
        brand_text_layout.addWidget(nav_subtitle)

        top_header_layout.addWidget(self.brand_logo_label)
        top_header_layout.addWidget(brand_text_box)
        top_header_layout.addStretch()
        root_layout.addWidget(top_header)

        body = QWidget()
        body.setObjectName("appBody")
        outer = QHBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)
        root_layout.addWidget(body, 1)

        # Left navigation: icon-only compact rail.
        left_column = QWidget()
        left_column.setObjectName("leftColumn")
        left_column.setFixedWidth(68)
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        nav_panel = QWidget()
        nav_panel.setObjectName("navPanel")
        nav_panel.setFixedWidth(68)
        nav_layout = QVBoxLayout(nav_panel)
        # Keep the rail itself at 60 px, but center every navigation control
        # explicitly. This avoids platform/style metrics shifting the buttons
        # a few pixels left or right.
        nav_layout.setContentsMargins(0, 12, 0, 10)
        nav_layout.setSpacing(10)
        nav_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        left_layout.addWidget(nav_panel, 1)

        # Raised tiles, painted rather than styled — Qt stylesheets cannot draw
        # the two opposed shadows the effect needs. See tracyy.ui.surfaces.
        self.nav_file = RaisedIconButton(tile=44, radius=15)
        self.nav_settings = RaisedIconButton(tile=44, radius=15)
        self.nav_server = RaisedIconButton(tile=44, radius=15)
        self.nav_sync = RaisedIconButton(tile=44, radius=15)

        self.nav_file.setToolTip("File")
        self.nav_settings.setToolTip("Settings")
        self.nav_server.setToolTip("Server")
        self.nav_sync.setToolTip("Sync")

        self.nav_file.setObjectName("sidebarNavButton")
        self.nav_settings.setObjectName("sidebarNavButton")
        self.nav_server.setObjectName("sidebarNavButton")
        self.nav_sync.setObjectName("sidebarNavButton")

        self.nav_file.setIcon(self.navigation_icon("file"))
        self.nav_settings.setIcon(self.navigation_icon("settings"))
        self.nav_server.setIcon(self.navigation_icon("server"))
        self.nav_sync.setIcon(self.navigation_icon("sync"))

        for navigation_button in (
            self.nav_file,
            self.nav_settings,
            self.nav_server,
            self.nav_sync,
        ):
            navigation_button.setAutoRaise(False)
            navigation_button.setIconSize(QSize(20, 20))
            navigation_button.setSizePolicy(
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Fixed,
            )

        self.nav_file.setCheckable(True)
        self.nav_settings.setCheckable(True)
        self.nav_server.setCheckable(True)
        self.nav_sync.setCheckable(True)
        self.nav_file.setChecked(True)

        nav_layout.addWidget(
            self.nav_file,
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )
        nav_layout.addWidget(
            self.nav_settings,
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )
        nav_layout.addWidget(
            self.nav_server,
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )
        nav_layout.addWidget(
            self.nav_sync,
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )
        nav_layout.addStretch()

        outer.addWidget(left_column)

        self.license_status_button = QPushButton()
        self.license_status_button.setObjectName("licenseStatusButton")
        self.license_status_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.license_status_button.setFixedSize(42, 34)
        self._compact_sidebar = True
        self.license_status_button.setToolTip("Open License Information")
        self.license_status_button.clicked.connect(self.show_license_information)

        self.license_status_label = self.license_status_button
        nav_layout.addWidget(
            self.license_status_button,
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )

        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)

        # FILE PAGE
        file_page = QWidget()
        file_outer = QHBoxLayout(file_page)
        file_outer.setContentsMargins(2, 0, 0, 0)
        file_outer.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(4)
        file_outer.addWidget(splitter)

        playlist_panel = QWidget()
        playlist_panel.setMinimumWidth(320)
        playlist_panel.setMaximumWidth(430)
        playlist_layout = QVBoxLayout(playlist_panel)
        playlist_layout.setContentsMargins(0, 22, 0, 0)
        playlist_layout.setSpacing(8)

        self.playlist_load_progress_label = QLabel("WAVEFORM — 0/0")
        self.playlist_load_progress_label.setObjectName("playlistLoadProgress")
        self.playlist_load_progress_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.playlist_load_progress_label.setMinimumHeight(24)
        self.playlist_load_progress_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        playlist_layout.addWidget(self.playlist_load_progress_label)

        # Project / Output FPS belongs to the File workflow because it changes
        # cue timecode and every track Offset shown in the Playlist. Input LTC
        # FPS remains a separate decoder setting on the Settings page.
        file_fps_row = QHBoxLayout()
        file_fps_row.setContentsMargins(0, 0, 0, 0)
        file_fps_row.setSpacing(8)
        file_fps_label = QLabel("FPS")
        file_fps_label.setObjectName("fileFpsLabel")
        self.fps = QComboBox()
        self.fps.setObjectName("projectFpsCombo")
        self.fps.addItems(["24", "25", "30"])
        self.fps.setCurrentText("30")
        self.fps.setMaximumWidth(92)
        # Hand the combo an explicit QListView. Without it macOS uses its own
        # popup, which ignores the stylesheet and sizes itself from the closed
        # control — the reason 24/25/30 rendered as single clipped digits.
        self.fps.setView(QListView())
        self.fps.view().setMinimumWidth(110)
        self.fps.setToolTip(
            "Project / Output FPS. This controls cue timecode, Playlist "
            "Offset display and LTC output. Input LTC has its own FPS in Settings."
        )
        self._input_timecode_project_fps = int(self.fps.currentText())
        self.fps.currentTextChanged.connect(lambda _value: self.refresh_cue_table())
        self.fps.currentTextChanged.connect(self.on_input_timecode_fps_changed)
        file_fps_row.addWidget(file_fps_label)
        file_fps_row.addStretch(1)
        file_fps_row.addWidget(self.fps)
        playlist_layout.addLayout(file_fps_row)

        self.playlist = PlaylistDropListWidget()
        self.playlist.setAlternatingRowColors(False)
        # Every row is drawn by PlaylistCardDelegate, so the stylesheet only
        # has to supply the dark pane the glass cards sit on. Item rules are
        # deliberately absent: a background or border here would paint over
        # the cards, and a selection colour would fight the one they draw.
        self.playlist.setStyleSheet(
            """
            QTableWidget#playlistTable {
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid rgba(255, 255, 255, 0.07);
                border-radius: 14px;
                outline: none;
                padding: 4px;
                selection-background-color: transparent;
                selection-color: #FFFFFF;
            }
            QTableWidget#playlistTable::item {
                background: transparent;
                border: none;
                padding: 0px;
            }
            """
        )
        # Drag/drop policy is owned by PlaylistDropListWidget. Do not
        # overwrite it here with QTableWidget.InternalMove; that mode can
        # consume source cells before our row-level reorder has finished.
        self.playlist.cellClicked.connect(self.on_playlist_cell_clicked)
        self.playlist.cellDoubleClicked.connect(self.on_playlist_cell_double_clicked)
        self.playlist.itemChanged.connect(self.on_playlist_item_changed)
        self.playlist.rows_reordered.connect(self.on_playlist_rows_reordered)
        self.playlist_rename_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_F2),
            self.playlist,
        )
        self.playlist_rename_shortcut.activated.connect(self.rename_current_playlist_item)
        playlist_layout.addWidget(self.playlist, 1)

        playlist_action_buttons = QHBoxLayout()
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Remove the selected track from the playlist.")
        playlist_action_buttons.addWidget(self.remove_button)
        playlist_action_buttons.addStretch()
        playlist_layout.addLayout(playlist_action_buttons)

        project_buttons = QHBoxLayout()

        self.save_project_button = QPushButton("Save Project")
        self.save_project_button.setToolTip("Save the current Tracyy project.")

        self.load_project_button = QPushButton("Load Project")
        self.load_project_button.setToolTip("Load a Tracyy project.")

        self.relink_media_button = QPushButton("Relink Media")
        self.relink_media_button.setToolTip(
            "Locate missing audio files without losing Cue or Offset data."
        )

        project_buttons.addWidget(self.save_project_button)
        project_buttons.addWidget(self.load_project_button)
        project_buttons.addWidget(self.relink_media_button)
        playlist_layout.addLayout(project_buttons)

        self.auto_backup_checkbox = QCheckBox("Auto Backup — every 15 min")
        self.auto_backup_checkbox.setObjectName("autoBackupCheckbox")
        self.auto_backup_checkbox.setToolTip(
            "When enabled, Tracyy writes timestamped .Tracyy backups every "
            "15 minutes into a Backup folder beside the current project."
        )
        self.auto_backup_checkbox.toggled.connect(self.on_auto_backup_toggled)
        playlist_layout.addWidget(self.auto_backup_checkbox)

        splitter.addWidget(playlist_panel)

        player_panel = QWidget()
        layout = QVBoxLayout(player_panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        cue_type_container = QWidget()
        cue_type_container_layout = QVBoxLayout(cue_type_container)
        cue_type_container_layout.setContentsMargins(0, 10, 0, 10)
        cue_type_container_layout.setSpacing(6)

        cue_type_strip = QWidget()
        cue_type_strip_layout = QHBoxLayout(cue_type_strip)
        cue_type_strip_layout.setContentsMargins(0, 6, 0, 6)
        cue_type_strip_layout.setSpacing(8)

        self.cue_type_content = QWidget()
        self.cue_type_content.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Fixed,
        )
        self.cue_type_row = QHBoxLayout(self.cue_type_content)
        self.cue_type_row.setContentsMargins(0, 6, 0, 6)
        self.cue_type_row.setSpacing(8)
        self.cue_type_buttons = {}

        self.cue_type_scroll = QScrollArea()
        self.cue_type_scroll.setWidgetResizable(False)
        self.cue_type_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.cue_type_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.cue_type_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cue_type_scroll.setFixedHeight(72)
        self.cue_type_scroll.setWidget(self.cue_type_content)

        self.cue_type_more_button = QPushButton("⋯")
        self.cue_type_more_button.setFixedSize(38, 34)
        self.cue_type_more_button.setToolTip("Customize Cue Point Types")

        cue_type_strip_layout.addWidget(self.cue_type_scroll, 1)
        cue_type_strip_layout.addWidget(
            self.cue_type_more_button,
            0,
            Qt.AlignmentFlag.AlignTop,
        )

        cue_type_container_layout.addWidget(cue_type_strip)

        self.rebuild_cue_type_buttons()
        layout.addWidget(cue_type_container)

        self.top_dock = QGroupBox("Timeline")
        self.top_dock.setObjectName("topDock")
        top_layout = QVBoxLayout(self.top_dock)

        self.tc_container = QWidget()
        self.tc_container.setObjectName("tcContainer")
        self.tc_stack = QStackedLayout(self.tc_container)
        self.tc_stack.setContentsMargins(0, 0, 0, 0)

        self.tc_label = QLabel("00:00:00:00")
        self.tc_label.setObjectName("tc")
        self.tc_label.setToolTip("")
        self.tc_label.setCursor(Qt.CursorShape.ArrowCursor)

        self.tc_editor = QLineEdit("00:00:00:00")
        self.tc_editor.setObjectName("tcEditor")
        self.tc_editor.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.tc_editor.setInputMask("00:00:00:00")
        self.tc_editor.hide()

        self.tc_stack.addWidget(self.tc_label)
        self.tc_stack.addWidget(self.tc_editor)

        self.clock_label = QLabel("00:00:00 / 00:00:00")
        self.status_label = QLabel("STOPPED")
        self.now_playing = QLabel("No track selected")
        self.now_playing.setObjectName("nowPlaying")

        self.clock_label.hide()
        self.status_label.hide()
        self.now_playing.hide()

        self.tc_container.hide()

        self.main_waveform = WaveformWidget()
        self.main_waveform.seek_requested.connect(self.seek_from_waveform)
        self.main_waveform.marker_move_finished.connect(self.on_waveform_marker_moved)

        waveform_controls = QHBoxLayout()
        self.wave_follow = QPushButton("Follow")
        self.wave_follow.setCheckable(True)
        self.wave_follow.setChecked(True)
        self.wave_zoom_in = QPushButton("Zoom +")
        self.wave_zoom_out = QPushButton("Zoom −")
        self.wave_fit = QPushButton("Fit")
        waveform_controls.addStretch()

        top_layout.addWidget(self.main_waveform)

        timeline_toolbar = QHBoxLayout()
        timeline_toolbar.setContentsMargins(2, 0, 2, 0)
        timeline_toolbar.setSpacing(6)

        # Track Offset is edited directly in the Playlist table. The timeline
        # toolbar is reserved for waveform navigation only.
        timeline_toolbar.addStretch()
        timeline_toolbar.addWidget(self.wave_follow)
        timeline_toolbar.addWidget(self.wave_zoom_in)
        timeline_toolbar.addWidget(self.wave_zoom_out)
        timeline_toolbar.addWidget(self.wave_fit)
        top_layout.addLayout(timeline_toolbar)

        self.standby_panel = QWidget()
        standby_layout = QVBoxLayout(self.standby_panel)
        standby_layout.setContentsMargins(0, 3, 0, 0)
        standby_layout.setSpacing(2)

        self.standby_title = QLabel("")
        self.standby_title.setObjectName("standbyTitle")
        standby_layout.addWidget(self.standby_title)

        self.standby_waveform = WaveformWidget()
        self.standby_waveform.setMinimumHeight(86)
        self.standby_waveform.setMaximumHeight(96)
        self.standby_waveform.auto_follow = False
        self.standby_waveform.setEnabled(False)
        standby_layout.addWidget(self.standby_waveform)

        self.standby_panel.setVisible(False)
        top_layout.addWidget(self.standby_panel)

        layout.addWidget(self.top_dock, 0)

        self.timeline_loading_view = VirtualCueLoadingWidget()
        self.timeline_loading_view.setObjectName("timelineLoadingView")
        # Added further down, wrapped together with the transport bar so the
        # list can run underneath it.

        cue_group = QGroupBox("Cue / Marker Editor")
        cue_layout = QVBoxLayout(cue_group)

        sidebar_time_row = QHBoxLayout()
        sidebar_time_title = QLabel("Cue Time")
        sidebar_time_title.setObjectName("sidebarTimeTitle")
        self.cue_time_editor = QLabel("00:00:00:00 / 00:00:00:00")
        self.cue_time_editor.setObjectName("sidebarTcLabel")
        self.cue_time_editor.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.cue_time_editor.setMinimumWidth(245)
        sidebar_time_row.addWidget(sidebar_time_title)
        sidebar_time_row.addStretch()
        sidebar_time_row.addWidget(self.cue_time_editor)
        cue_layout.addLayout(sidebar_time_row)

        cue_controls = QHBoxLayout()
        self.add_cue_button = QPushButton("Add Cue at Playhead")
        self.import_cue_button = QPushButton("Import Cue")
        self.export_cue_button = QPushButton("Export Cue")
        cue_controls.addWidget(self.add_cue_button)
        cue_controls.addWidget(self.import_cue_button)
        cue_controls.addWidget(self.export_cue_button)
        cue_controls.addStretch()
        cue_layout.addLayout(cue_controls)

        # Who is typing in which cell. The outline in the table carries the
        # colour; this carries the name, because the LABEL column is about
        # 150px wide and a name badge inside it would cover the text somebody
        # is in the middle of writing. Hidden until there is something to say
        # so it costs no height on a machine working alone.
        self.cue_presence_status = QLabel("")
        self.cue_presence_status.setObjectName("cuePresenceStatus")
        self.cue_presence_status.setTextFormat(Qt.TextFormat.RichText)
        self.cue_presence_status.hide()
        cue_layout.addWidget(self.cue_presence_status)

        self.cue_table = QTableWidget(0, 4)
        self.cue_table_delegate = CueTableDelegate(self.cue_table)
        self.cue_table.setItemDelegate(self.cue_table_delegate)
        self.cue_table_delegate.editor_started.connect(self.on_cue_table_editor_started)
        self.cue_table_delegate.closeEditor.connect(self.on_cue_table_editor_closed)
        self.cue_table.setHorizontalHeaderLabels(["TIME", "CUE TYPE", "CUE", "LABEL"])
        self.cue_table.verticalHeader().setVisible(False)
        self.cue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cue_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.cue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cue_table.setAlternatingRowColors(False)
        self.cue_table.setMouseTracking(True)

        for watched_widget in (
            self.cue_table,
            self.cue_table.viewport(),
            self.cue_table.horizontalHeader(),
            self.cue_table.verticalHeader(),
            self.cue_table.horizontalScrollBar(),
            self.cue_table.verticalScrollBar(),
        ):
            watched_widget.installEventFilter(self)
        self.cue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.cue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.cue_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.cue_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.cue_table.setMinimumWidth(370)
        self.cue_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cue_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.cue_table.setStyleSheet(
            """
            QTableWidget {
                font-size: 12px;
                background: #070A05;
                color: #E9E9E9;
                border: 1px solid #223313;
                border-radius: 10px;
                gridline-color: #243515;
                selection-background-color: #242A22;
                outline: none;
            }
            QTableWidget::item {
                background: #0B1007;
                color: #E1E5DA;
                padding: 4px 4px;
                border-bottom: 1px solid #223313;
            }
            QTableWidget::item:selected {
                background: #242A22;
            }
            QTableWidget::item:hover {
                background: #162509;
            }
            QHeaderView::section {
                font-size: 12px;
                font-weight: 600;
                color: #F2F2F2;
                background: #142307;
                border: none;
                border-bottom: 1px solid #315017;
                padding: 6px 5px;
            }
            QTableWidget QLineEdit {
                font-size: 12px;
                padding: 0px 4px;
                margin: 0px;
                background: #17270A;
                color: #FFFFFF;
                border: 1px solid #3E641D;
                border-radius: 5px;
            }
            """
        )
        self.cue_table.verticalHeader().setDefaultSectionSize(40)
        self.cue_table.horizontalHeader().setStretchLastSection(False)
        self.cue_table.horizontalHeader().setMinimumSectionSize(40)
        self.update_cue_table_column_widths()
        # Sits on the viewport, above the rows, transparent to input.
        self.cue_presence_overlay = CueTablePresenceOverlay(self.cue_table)
        cue_layout.addWidget(self.cue_table, 1)

        self.wave_follow.toggled.connect(
            lambda checked: setattr(self.main_waveform, "auto_follow", checked)
        )
        self.wave_zoom_in.clicked.connect(self.main_waveform.zoom_in)
        self.wave_zoom_out.clicked.connect(self.main_waveform.zoom_out)
        self.wave_fit.clicked.connect(self.main_waveform.fit_view)

        # Live Music Level belongs to the Transport page.
        self.music_level = QSlider(Qt.Orientation.Horizontal)
        self.music_level.setRange(0, 150)
        self.music_level.setValue(100)
        self.music_level_value = QLabel("100 %")

        transport_group = QGroupBox("")
        transport_group.setObjectName("transportGroupBar")
        transport_layout = QVBoxLayout(transport_group)
        transport_layout.setContentsMargins(18, 12, 18, 12)
        transport_layout.setSpacing(9)

        transport_header = QHBoxLayout()
        transport_header.setContentsMargins(0, 0, 0, 0)
        transport_header.setSpacing(10)

        self.transport_elapsed_label = QLabel("00:00")
        self.transport_elapsed_label.setObjectName("transportMiniTime")
        self.transport_elapsed_label.setMinimumWidth(56)

        self.transport_title_label = AdaptiveWrapLabel(
            "NO TRACK",
            base_point_size=12,
            min_point_size=8,
            max_lines=2,
        )
        self.transport_title_label.setObjectName("transportTrackTitle")
        self.transport_title_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
        )

        self.transport_duration_label = QLabel("00:00")
        self.transport_duration_label.setObjectName("transportMiniTimeMuted")
        self.transport_duration_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.transport_duration_label.setMinimumWidth(56)

        self.transport_title_label.setMinimumHeight(26)
        self.transport_title_label.setMaximumHeight(40)

        transport_header.addWidget(self.transport_elapsed_label)
        transport_header.addWidget(self.transport_title_label, 1)
        transport_header.addWidget(self.transport_duration_label)
        transport_layout.addLayout(transport_header)

        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.setObjectName("transportTimeline")
        self.position.setRange(0, 100000)
        transport_layout.addWidget(self.position)

        # Three columns of equal stretch: the cluster sits in the middle one and
        # is therefore centred on the panel, not on whatever space the volume
        # group happens to leave over.
        controls = QGridLayout()
        controls.setContentsMargins(0, 2, 0, 0)
        # Zero, because a grid inserts spacing only between columns that both
        # hold something. Column 0 is the empty counterweight, so it would get
        # no gap while column 2 got one — and the cluster would sit half a gap
        # left of centre. The 8px rhythm lives inside the nested rows instead.
        controls.setHorizontalSpacing(0)

        self.previous_button = QPushButton("⏮")
        self.previous_button.setObjectName("transportIconButton")
        self.previous_button.setToolTip("Previous  (←)")

        self.play_pause_button = QPushButton("▶")
        self.play_pause_button.setObjectName("transportPlayButton")
        self.play_pause_button.setToolTip("Play / Pause  (Space)")

        self.next_button = QPushButton("⏭")
        self.next_button.setObjectName("transportIconButton")
        self.next_button.setToolTip("Next  (→)")

        self.stop_button = QPushButton("■")
        self.stop_button.setObjectName("transportIconButton")
        self.stop_button.setToolTip("Stop")

        self.zero_button = QPushButton("↶")
        self.zero_button.setObjectName("transportIconButton")
        self.zero_button.setToolTip("Back to Start")

        cluster = QHBoxLayout()
        cluster.setContentsMargins(0, 0, 0, 0)
        cluster.setSpacing(8)
        cluster.addWidget(self.zero_button)
        cluster.addWidget(self.previous_button)
        cluster.addWidget(self.play_pause_button)
        cluster.addWidget(self.next_button)
        cluster.addWidget(self.stop_button)
        controls.addLayout(cluster, 0, 1)

        self.transport_volume_label = QLabel("VOL")
        self.transport_volume_label.setObjectName("transportVolumeLabel")
        self.music_level.setObjectName("transportVolumeSlider")
        self.music_level.setMinimumWidth(170)
        self.music_level.setMaximumWidth(220)
        self.music_level_value.setObjectName("transportVolumeValue")
        self.music_level_value.setMinimumWidth(44)
        self.music_level_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        # Nested layouts rather than container widgets: a bare QWidget here
        # paints the application background over the translucent panel and
        # leaves a black block where the glass should be.
        volume = QHBoxLayout()
        volume.setContentsMargins(0, 0, 0, 0)
        volume.setSpacing(8)
        volume.addStretch(1)
        volume.addWidget(self.transport_volume_label)
        volume.addWidget(self.music_level)
        volume.addWidget(self.music_level_value)
        controls.addLayout(volume, 0, 2)

        # Column 0 stays empty on purpose. It is the counterweight that makes
        # column 2's width, and so the middle column's position, symmetrical.
        controls.setColumnStretch(0, 1)
        controls.setColumnStretch(1, 0)
        controls.setColumnStretch(2, 1)

        transport_layout.addLayout(controls)

        self.transport_overlay_host = OverlayStackWidget(
            self.timeline_loading_view,
            transport_group,
        )
        layout.addWidget(self.transport_overlay_host, 1)

        note = QLabel("Output Matrix → gán Music L, Music R, LTC hoặc Off cho từng channel.")
        note.setWordWrap(True)
        layout.addWidget(note, 0)

        splitter.addWidget(player_panel)
        splitter.setSizes([350, 1350])

        cue_group.setMinimumWidth(390)
        cue_group.setMaximumWidth(620)
        splitter.addWidget(cue_group)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([220, 740, 430])
        self.pages.addWidget(file_page)

        # SETTINGS PAGE
        settings_page = QWidget()
        settings_layout = QVBoxLayout(settings_page)

        settings_title = QLabel("SETTINGS")
        settings_title.setObjectName("settingsTitle")
        settings_layout.addWidget(settings_title)

        input_timecode_group = QGroupBox("Input Timecode Chase — LTC Audio")
        input_timecode_group.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        input_timecode_layout = QVBoxLayout(input_timecode_group)

        input_timecode_note = QLabel(
            "External LTC controls the Tracyy clock, active track, playhead "
            "and cues. Music and LTC output remain OFF while Chase is enabled."
        )
        input_timecode_note.setWordWrap(True)
        input_timecode_layout.addWidget(input_timecode_note)

        self.input_timecode_enable = QCheckBox("Enable Input Timecode Chase")
        self.input_timecode_enable.setObjectName("inputTimecodeEnable")
        input_timecode_layout.addWidget(self.input_timecode_enable)

        input_device_form = QFormLayout()
        self.input_timecode_device = QComboBox()
        self.input_timecode_device.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.input_timecode_channel = QComboBox()
        self.input_timecode_channel.setMinimumWidth(150)

        self.input_timecode_fps = QComboBox()
        self.input_timecode_fps.setObjectName("inputTimecodeFPS")
        self.input_timecode_fps.addItems(
            [
                "24",
                "25",
                "30",
            ]
        )
        self.input_timecode_fps.setCurrentText(str(self._input_timecode_fps_default))
        self.input_timecode_fps.setToolTip(
            "FPS used only by the LTC input decoder. "
            "This does not change the Project/Output FPS."
        )

        self.input_timecode_latency_ms = QDoubleSpinBox()
        self.input_timecode_latency_ms.setObjectName("inputTimecodeLatency")
        self.input_timecode_latency_ms.setRange(
            -5000.0,
            5000.0,
        )
        self.input_timecode_latency_ms.setDecimals(1)
        self.input_timecode_latency_ms.setSingleStep(1.0)
        self.input_timecode_latency_ms.setValue(self._input_timecode_latency_ms_default)
        self.input_timecode_latency_ms.setSuffix(" ms")
        self.input_timecode_latency_ms.setToolTip(
            "Manual input timecode compensation. "
            "Negative values move the received timecode backward; "
            "positive values move it forward."
        )

        self.input_timecode_pre_roll = QDoubleSpinBox()
        self.input_timecode_pre_roll.setObjectName("inputTimecodePreRoll")
        self.input_timecode_pre_roll.setRange(
            0.0,
            10.0,
        )
        self.input_timecode_pre_roll.setDecimals(2)
        self.input_timecode_pre_roll.setSingleStep(0.05)
        self.input_timecode_pre_roll.setValue(self._input_timecode_pre_roll_default)
        self.input_timecode_pre_roll.setSuffix(" s")
        self.input_timecode_pre_roll.setToolTip(
            "Highlight and prepare matching tracks before their exact offset."
        )

        self.input_timecode_after_roll = QDoubleSpinBox()
        self.input_timecode_after_roll.setObjectName("inputTimecodeAfterRoll")
        self.input_timecode_after_roll.setRange(
            0.0,
            10.0,
        )
        self.input_timecode_after_roll.setDecimals(2)
        self.input_timecode_after_roll.setSingleStep(0.05)
        self.input_timecode_after_roll.setValue(self._input_timecode_after_roll_default)
        self.input_timecode_after_roll.setSuffix(" s")
        self.input_timecode_after_roll.setToolTip(
            "Bridge short LTC gaps and keep the active track through boundaries."
        )

        input_device_form.addRow(
            "LTC Input Device",
            self.input_timecode_device,
        )
        input_device_form.addRow(
            "Input Channel",
            self.input_timecode_channel,
        )
        input_device_form.addRow(
            "Input FPS",
            self.input_timecode_fps,
        )
        input_device_form.addRow(
            "Input Latency",
            self.input_timecode_latency_ms,
        )
        input_device_form.addRow(
            "Input Pre-roll",
            self.input_timecode_pre_roll,
        )
        input_device_form.addRow(
            "Input After-roll",
            self.input_timecode_after_roll,
        )
        input_timecode_layout.addLayout(input_device_form)

        self.input_timecode_status_label = QLabel("INPUT TIMECODE OFF")
        self.input_timecode_status_label.setObjectName("inputTimecodeStatus")
        self.input_timecode_status_label.setProperty("state", "off")
        self.input_timecode_status_label.setMinimumHeight(32)
        input_timecode_layout.addWidget(self.input_timecode_status_label)

        input_timecode_test_row = QHBoxLayout()
        self.input_timecode_edit = QLineEdit("01:00:00:00")
        self.input_timecode_edit.setObjectName("inputTimecodeEdit")
        self.input_timecode_edit.setInputMask("00:00:00:00")
        self.input_timecode_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.input_timecode_edit.setMaximumWidth(180)
        self.input_timecode_edit.setToolTip(
            "Inject one test timecode frame without an LTC generator."
        )
        self.input_timecode_button = QPushButton("Test Frame")
        self.input_timecode_button.setObjectName("inputTimecodeButton")
        self.input_timecode_button.setMinimumWidth(120)
        input_timecode_test_row.addWidget(QLabel("Test TC"))
        input_timecode_test_row.addWidget(self.input_timecode_edit)
        input_timecode_test_row.addWidget(self.input_timecode_button)
        input_timecode_test_row.addStretch()
        input_timecode_layout.addLayout(input_timecode_test_row)

        # "&&" or Qt eats the ampersand as a mnemonic and the title reads
        # "Output Devices _Channel Routing" with C underlined.
        output_group = QGroupBox("Output Devices && Channel Routing")
        output_group.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        output_layout = QVBoxLayout(output_group)

        output_note = QLabel(
            "Add one or more soundcards. Each device has its own output "
            "channels and routing assignment."
        )
        output_note.setWordWrap(True)
        output_layout.addWidget(output_note)

        output_toolbar = QHBoxLayout()

        self.add_output_device_button = QPushButton("+ Add Output Device")
        self.add_output_device_button.setToolTip(
            "Add another soundcard and configure its channels."
        )

        output_toolbar.addWidget(self.add_output_device_button)
        output_toolbar.addStretch()
        output_layout.addLayout(output_toolbar)

        self.output_device_scroll = QScrollArea()
        self.output_device_scroll.setWidgetResizable(True)
        self.output_device_scroll.setFrameShape(QFrame.Shape.StyledPanel)

        self.output_device_container = QWidget()
        self.output_device_rows_layout = QVBoxLayout(self.output_device_container)
        self.output_device_rows_layout.setContentsMargins(
            8,
            8,
            8,
            8,
        )
        self.output_device_rows_layout.setSpacing(10)
        self.output_device_rows_layout.addStretch(1)

        self.output_device_scroll.setWidget(self.output_device_container)
        output_layout.addWidget(
            self.output_device_scroll,
            1,
        )

        self.output_device_rows: list[dict[str, object]] = []

        self.ltc_level = QSlider(Qt.Orientation.Horizontal)
        self.ltc_level.setRange(
            5,
            100,
        )
        self.ltc_level.setValue(65)
        self.ltc_level_value = QLabel("65 %")

        bottom_form = QFormLayout()
        bottom_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        ltc_row = QHBoxLayout()
        ltc_row.addWidget(
            self.ltc_level,
            1,
        )
        ltc_row.addWidget(self.ltc_level_value)

        bottom_form.addRow(
            "LTC Level",
            ltc_row,
        )
        output_layout.addLayout(bottom_form)

        device_buttons = QHBoxLayout()

        self.refresh_button = QPushButton("Rescan Devices")
        self.refresh_button.setToolTip(
            "Re-read the sound cards from the operating system. "
            "Use this after unplugging or plugging in an interface."
        )
        # No Apply button: routing is pushed to the transport the moment it
        # changes. Assignments are read by the audio callback on every block,
        # so they take effect on the next one with no stream reopen — there was
        # never anything for an Apply step to do except delay it. Changing the
        # soundcard still reopens the stream, but that happens on the change.
        device_buttons.addWidget(self.refresh_button)
        device_buttons.addStretch()

        output_layout.addLayout(device_buttons)

        # Output routing and Input Timecode Chase are two independent jobs, and
        # an operator setting up a show reads across them rather than scrolling
        # between them. Equal columns: output left, input right.
        settings_columns = QHBoxLayout()
        settings_columns.setContentsMargins(0, 0, 0, 0)
        settings_columns.setSpacing(12)

        settings_output_column = QVBoxLayout()
        settings_output_column.setContentsMargins(0, 0, 0, 0)
        settings_output_column.addWidget(output_group, 1)

        # The input side is a form and stops at its natural height; the stretch
        # keeps it pinned to the top instead of stretching its rows apart.
        settings_input_column = QVBoxLayout()
        settings_input_column.setContentsMargins(0, 0, 0, 0)
        settings_input_column.addWidget(input_timecode_group)
        settings_input_column.addStretch(1)

        settings_columns.addLayout(settings_output_column, 1)
        settings_columns.addLayout(settings_input_column, 1)
        settings_layout.addLayout(settings_columns, 1)
        self.pages.addWidget(settings_page)

        # SERVER PAGE
        server_page = QWidget()
        server_layout = QVBoxLayout(server_page)

        server_title = QLabel("Cue Viewer Servers")
        server_title.setObjectName("serverPageTitle")
        server_subtitle = QLabel(
            "Crew mở địa chỉ này trên điện thoại để xem cue theo thời gian thực."
        )
        server_subtitle.setObjectName("serverPageSubtitle")
        server_subtitle.setWordWrap(True)
        server_layout.addWidget(server_title)
        server_layout.addWidget(server_subtitle)

        server_toolbar = QHBoxLayout()
        # Remove moved onto each card. A toolbar Remove acted on the table's
        # selected row, and the selection was never visible enough to be sure
        # which server you were about to delete.
        self.add_server_button = QPushButton("+ New Server")
        self.add_server_button.setObjectName("serverPrimaryButton")
        self.refresh_server_networks_button = QPushButton("Refresh Networks")
        server_toolbar.addStretch()
        server_toolbar.addWidget(self.refresh_server_networks_button)
        server_toolbar.addWidget(self.add_server_button)
        server_layout.addLayout(server_toolbar)

        # Cards, not a table. Each server is one object carrying seven
        # different kinds of control — a name field, a port field, a network
        # combo, a cue-type picker, an address, a state and a switch — and a
        # table row is the wrong container for that. The address is what crew
        # actually need, so it gets the size a table cell could never give it.
        self.server_cards_scroll = QScrollArea()
        self.server_cards_scroll.setObjectName("serverCardsScroll")
        self.server_cards_scroll.setWidgetResizable(True)
        self.server_cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.server_cards_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.server_cards_container = QWidget()
        self.server_cards_container.setObjectName("serverCardsContainer")
        self.server_cards_layout = QGridLayout(self.server_cards_container)
        self.server_cards_layout.setContentsMargins(0, 4, 0, 4)
        self.server_cards_layout.setHorizontalSpacing(16)
        self.server_cards_layout.setVerticalSpacing(16)

        self.server_cards_scroll.setWidget(self.server_cards_container)
        server_layout.addWidget(self.server_cards_scroll, 1)

        # One line, above the cards. The old four-sentence block sat at the
        # very bottom of the page, as far from the controls it explains as the
        # layout allowed; the "stop before changing port" half of it now
        # appears on the card that is actually running.
        self.server_help = QLabel(
            "Auto — All Networks cho phép truy cập qua mọi card mạng; "
            "chọn một IPv4 để khoá viewer vào đúng mạng show."
        )
        self.server_help.setObjectName("serverPageHelp")
        self.server_help.setWordWrap(True)
        server_layout.insertWidget(2, self.server_help)
        self.pages.addWidget(server_page)

        self.pages.addWidget(self.build_sync_page())

        self.nav_file.clicked.connect(lambda: self.switch_page(0))
        self.nav_settings.clicked.connect(lambda: self.switch_page(1))
        self.nav_server.clicked.connect(lambda: self.switch_page(2))
        self.nav_sync.clicked.connect(lambda: self.switch_page(3))

        self.add_server_button.clicked.connect(self.add_server_profile)
        self.refresh_server_networks_button.clicked.connect(self.refresh_server_networks)
        QTimer.singleShot(0, self.add_server_profile)
        QTimer.singleShot(
            0,
            self.apply_license_state,
        )
        self.playlist.external_paths_dropped.connect(self.add_dropped_playlist_paths)
        self.remove_button.clicked.connect(self.remove_selected)
        self.save_project_button.clicked.connect(self.save_project)
        self.load_project_button.clicked.connect(self.load_project)
        self.relink_media_button.clicked.connect(self.relink_missing_media_dialog)

        self.add_cue_button.clicked.connect(self.add_cue)
        self.import_cue_button.clicked.connect(self.import_cue_csv)
        self.export_cue_button.clicked.connect(self.export_cue_csv)
        self.cue_type_more_button.clicked.connect(self.open_cue_type_editor)
        self.cue_table.itemClicked.connect(self.on_cue_table_item_clicked)
        self.cue_table.itemDoubleClicked.connect(self.on_cue_table_item_double_clicked)
        self.cue_table.itemChanged.connect(self.on_cue_item_changed)
        self.cue_table.itemSelectionChanged.connect(self.sync_active_type_from_selection)
        self.set_active_cue_type(self.active_cue_type, apply_to_selected=False)

        self.refresh_button.clicked.connect(self.rescan_output_devices)
        self.add_output_device_button.clicked.connect(self.add_output_device_row)
        self.input_timecode_fps.currentTextChanged.connect(
            self.on_input_timecode_input_fps_changed
        )
        self.input_timecode_latency_ms.valueChanged.connect(
            self.on_input_timecode_latency_changed
        )
        self.input_timecode_pre_roll.valueChanged.connect(self.on_input_timecode_roll_changed)
        self.input_timecode_after_roll.valueChanged.connect(self.on_input_timecode_roll_changed)
        self.input_timecode_button.clicked.connect(self.apply_input_timecode)
        self.input_timecode_edit.returnPressed.connect(self.apply_input_timecode)
        self.input_timecode_enable.toggled.connect(self.set_input_timecode_enabled)
        self.input_timecode_device.currentIndexChanged.connect(
            self.on_input_timecode_device_changed
        )
        self.input_timecode_channel.currentIndexChanged.connect(
            self.on_input_timecode_channel_changed
        )
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        self.stop_button.clicked.connect(self.stop_playback)
        self.zero_button.clicked.connect(self.restart_from_beginning)
        self.previous_button.clicked.connect(self.play_previous)
        self.next_button.clicked.connect(self.play_next)

        self.refresh_transport_icons()

        self.position.sliderPressed.connect(self.begin_seek)
        self.position.sliderReleased.connect(self.end_seek)

        self.music_level.valueChanged.connect(self.on_music_level_changed)
        self.ltc_level.valueChanged.connect(self.on_ltc_level_changed)

        QTimer.singleShot(0, self.update_cue_table_column_widths)
        QTimer.singleShot(0, self.update_timeline_loading_geometry)

    def update_cue_table_column_widths(self) -> None:
        viewport_width = max(220, self.cue_table.viewport().width())
        total = max(1, viewport_width - 4)

        ratios = [0.23, 0.22, 0.16, 0.39]
        widths = [max(40, int(total * ratio)) for ratio in ratios]
        widths[-1] = max(
            60,
            total - widths[0] - widths[1] - widths[2],
        )

        for index, width in enumerate(widths):
            self.cue_table.setColumnWidth(index, width)

    def switch_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.nav_file.setChecked(index == 0)
        self.nav_settings.setChecked(index == 1)
        self.nav_server.setChecked(index == 2)
        self.nav_sync.setChecked(index == 3)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        try:
            self.update_cue_table_column_widths()
        except Exception:
            pass

        try:
            QTimer.singleShot(
                0,
                self.update_timeline_loading_geometry,
            )
        except Exception:
            pass

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def show_error(self, text: str) -> None:
        silent_message_box(self, "Playlist + LTC Player", text)

    def cue_table_contains_global_cursor(self) -> bool:
        global_position = self.cursor().pos()

        for widget in (
            self.cue_table,
            self.cue_table.viewport(),
            self.cue_table.horizontalHeader(),
            self.cue_table.verticalHeader(),
            self.cue_table.horizontalScrollBar(),
            self.cue_table.verticalScrollBar(),
        ):
            local_position = widget.mapFromGlobal(global_position)
            if widget.rect().contains(local_position):
                return True

        return False

    def release_cue_table_manual_browse(
        self,
        token: int,
    ) -> None:
        if token != self._cue_table_leave_token:
            return

        self._cue_table_mouse_inside = self.cue_table_contains_global_cursor()
        self._cue_table_manual_browse = False

        # LABEL hover/editor owns the table selection until interaction ends.
        self.resume_cue_table_follow_if_unlocked()
        timeline_position = self.current_timeline_media_position()
        self.update_next_cue_loading(timeline_position)

    def eventFilter(self, watched, event) -> bool:
        cue_table_widgets = (
            self.cue_table,
            self.cue_table.viewport(),
            self.cue_table.horizontalHeader(),
            self.cue_table.verticalHeader(),
            self.cue_table.horizontalScrollBar(),
            self.cue_table.verticalScrollBar(),
        )

        if watched in cue_table_widgets:
            event_type = event.type()

            if watched is self.cue_table.viewport():
                if event_type == QEvent.Type.MouseMove:
                    try:
                        point = event.position().toPoint()
                    except Exception:
                        point = event.pos()
                    index = self.cue_table.indexAt(point)
                    self.set_cue_table_label_hover(
                        bool(index.isValid() and index.column() == 3)
                    )
                elif event_type == QEvent.Type.Leave:
                    self.set_cue_table_label_hover(False)

            if event_type in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.Wheel,
            ):
                self._cue_table_leave_token += 1
                self._cue_table_mouse_inside = True
                self._cue_table_manual_browse = True
                browse_token = self._cue_table_leave_token
                QTimer.singleShot(
                    1200,
                    lambda current_token=browse_token: self.release_cue_table_manual_browse(
                        current_token
                    ),
                )

            elif event_type == QEvent.Type.Enter:
                self._cue_table_leave_token += 1
                self._cue_table_mouse_inside = True

            elif event_type == QEvent.Type.Leave:
                self._cue_table_leave_token += 1
                token = self._cue_table_leave_token

                # Delay avoids false Leave events while moving between
                # viewport, header and scrollbars.
                QTimer.singleShot(
                    250,
                    lambda current_token=token: self.release_cue_table_manual_browse(
                        current_token
                    ),
                )

        return super().eventFilter(watched, event)

    def show_master_conflict_dialog(self) -> None:
        return

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers()

        if key == Qt.Key.Key_Z and modifiers & Qt.KeyboardModifier.ControlModifier:
            self.undo_last_cue_action()
            event.accept()
            return

        if key == Qt.Key.Key_Left:
            self.play_previous()
            event.accept()
            return

        if key == Qt.Key.Key_Right:
            self.play_next()
            event.accept()
            return

        if key == Qt.Key.Key_Space:
            self.toggle_play_pause()
            event.accept()
            return

        if (
            key
            in (
                Qt.Key.Key_Delete,
                Qt.Key.Key_Backspace,
            )
            and self.cue_table.hasFocus()
        ):
            self.delete_selected_cue()
            event.accept()
            return

        super().keyPressEvent(event)

    def closeEvent(
        self,
        event,
    ) -> None:
        if self._shutdown_in_progress:
            event.accept()
            super().closeEvent(event)
            return

        # No project data changed since startup/load/save:
        # close immediately without showing the save dialog.
        if not self.project_has_unsaved_changes():
            self.perform_application_shutdown()
            event.accept()
            super().closeEvent(event)
            return

        action = self.confirm_project_close()

        if action == "cancel":
            event.ignore()
            return

        if action == "save":
            if not self.save_project_for_close():
                event.ignore()
                return

        self.perform_application_shutdown()
        event.accept()
        super().closeEvent(event)
