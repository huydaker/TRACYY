from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from tracyy import __version__ as tracyy_version
from tracyy.licensing.client import OFFLINE_GRACE_HOURS, LicenseResult
from tracyy.licensing.countdown import LicenseCountdownSnapshot, resolve_license_countdown
from tracyy.modules import LicensePolicy

if TYPE_CHECKING:  # pragma: no cover - import cycle, annotations only
    from tracyy.ui.main_window import MainWindow


class LicenseInformationDialog(QDialog):
    def __init__(
        self,
        main_window: MainWindow,
    ) -> None:
        super().__init__(main_window)
        self.main_window = main_window
        self.setWindowTitle("License Information")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setObjectName("licenseInformationDialog")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(18)

        title = QLabel("LICENSE INFORMATION")
        title.setObjectName("licenseInformationTitle")
        layout.addWidget(title)

        self.form = QFormLayout()
        self.form.setHorizontalSpacing(30)
        self.form.setVerticalSpacing(12)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.status_value = QLabel()
        self.source_value = QLabel()
        self.admin_usb_value = QLabel()
        self.connection_value = QLabel()
        self.last_online_value = QLabel()
        self.allowance_value = QLabel()
        self.remaining_value = QLabel()
        self.expiry_value = QLabel()
        self.machine_name_value = QLabel()
        self.machine_id_value = QLabel()

        self.remaining_value.setObjectName("licenseRemainingValue")

        rows = [
            ("License status:", self.status_value),
            ("License source:", self.source_value),
            ("Admin USB:", self.admin_usb_value),
            ("Connection status:", self.connection_value),
            ("Last online verification:", self.last_online_value),
            ("Offline allowance:", self.allowance_value),
            ("Time remaining:", self.remaining_value),
            ("Expires offline at:", self.expiry_value),
            ("Machine name:", self.machine_name_value),
            ("Machine ID:", self.machine_id_value),
        ]

        for label_text, value_label in rows:
            name_label = QLabel(label_text)
            name_label.setObjectName("licenseFieldName")
            value_label.setObjectName(value_label.objectName() or "licenseFieldValue")
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.form.addRow(name_label, value_label)

        layout.addLayout(self.form)

        self.message_label = QLabel()
        self.message_label.setObjectName("licenseMessage")
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        buttons = QHBoxLayout()

        self.scan_usb_button = QPushButton("Scan USB License")
        self.scan_usb_button.setObjectName("scanUsbLicenseButton")
        self.scan_usb_button.setMinimumWidth(170)
        self.scan_usb_button.setToolTip(
            "Scan mounted USB volumes and verify the encrypted "
            "Tracyy USB license without checking Google Sheet."
        )
        self.scan_usb_button.clicked.connect(self.scan_usb_license)
        buttons.addWidget(self.scan_usb_button)

        self.scan_sheet_button = QPushButton("Scan GG Sheet")
        self.scan_sheet_button.setObjectName("scanGoogleSheetButton")
        self.scan_sheet_button.setMinimumWidth(160)
        self.scan_sheet_button.setToolTip(
            "Connect to Google Sheet, retrieve the latest license "
            "status and save it for offline use."
        )
        self.scan_sheet_button.clicked.connect(self.scan_google_sheet_license)
        buttons.addWidget(self.scan_sheet_button)

        buttons.addStretch()

        close_button = QPushButton("Close")
        close_button.setMinimumWidth(100)
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self.refresh_values)
        self.countdown_timer.start()

        self.main_window.usb_license_scan_finished.connect(self.on_usb_license_scan_finished)
        self.main_window.google_sheet_scan_finished.connect(self.on_google_sheet_scan_finished)

        self._scan_feedback_message = ""
        self._scan_feedback_until = 0.0

        self.refresh_values()

    def scan_usb_license(
        self,
    ) -> None:
        self.set_scan_buttons_enabled(False)
        self.scan_usb_button.setText("Scanning USB…")
        self.set_scan_feedback(
            "Scanning mounted volumes, encrypted token and connected USB hardware serial…",
            seconds=60.0,
        )

        started = self.main_window.request_usb_license_scan()

        if not started:
            self.scan_usb_button.setText("Scan USB License")
            self.set_scan_buttons_enabled(True)
            self.set_scan_feedback(
                "Another license scan is already running.",
                seconds=6.0,
            )

    def on_usb_license_scan_finished(
        self,
        state: object,
        error: str,
    ) -> None:
        self.set_scan_buttons_enabled(True)
        self.scan_usb_button.setText("Scan USB License")

        self.refresh_values()

        if error:
            self.set_scan_feedback(
                "USB LICENSE SCAN FAILED — " + error,
                seconds=12.0,
            )
            return

        connected = bool(
            getattr(
                state,
                "connected",
                False,
            )
        )
        active = bool(
            getattr(
                state,
                "active",
                False,
            )
        )
        message = str(
            getattr(
                state,
                "message",
                "",
            )
            or ""
        )

        if connected:
            self.set_scan_feedback(
                "USB LICENSE VERIFIED — LICENSE BETA",
                seconds=10.0,
            )
        elif active:
            self.set_scan_feedback(
                message
                or (
                    "Admin USB is no longer connected. BETA remains active until Tracyy closes."
                ),
                seconds=10.0,
            )
        else:
            self.set_scan_feedback(
                "NO VALID USB LICENSE — "
                + (message or ("Check TRACYY_ADMIN/tracyy_admin_usb.lic and the USB serial.")),
                seconds=12.0,
            )

    def set_scan_buttons_enabled(
        self,
        enabled: bool,
    ) -> None:
        self.scan_usb_button.setEnabled(bool(enabled))
        self.scan_sheet_button.setEnabled(bool(enabled))

    def set_scan_feedback(
        self,
        message: str,
        *,
        seconds: float = 8.0,
    ) -> None:
        self._scan_feedback_message = str(message or "")
        self._scan_feedback_until = time.time() + max(
            0.0,
            float(seconds),
        )
        self.message_label.setText(self._scan_feedback_message)

    def scan_google_sheet_license(
        self,
    ) -> None:
        self.set_scan_buttons_enabled(False)
        self.scan_sheet_button.setText("Scanning Sheet…")
        self.set_scan_feedback(
            "Connecting to Google Sheet and retrieving the latest license status…",
            seconds=60.0,
        )

        started = self.main_window.request_google_sheet_license_scan()

        if not started:
            self.scan_sheet_button.setText("Scan GG Sheet")
            self.set_scan_buttons_enabled(True)
            self.set_scan_feedback(
                "Another license scan is already running.",
                seconds=6.0,
            )

    def on_google_sheet_scan_finished(
        self,
        result: object,
        error: str,
    ) -> None:
        self.set_scan_buttons_enabled(True)
        self.scan_sheet_button.setText("Scan GG Sheet")
        self.refresh_values()

        if error:
            self.set_scan_feedback(
                "GOOGLE SHEET SCAN FAILED — " + error,
                seconds=12.0,
            )
            return

        if result is None:
            self.set_scan_feedback(
                "GOOGLE SHEET SCAN FAILED — no result.",
                seconds=12.0,
            )
            return

        sheet_status = (
            str(
                getattr(
                    result,
                    "status",
                    "BLOCKED",
                )
                or "BLOCKED"
            )
            .strip()
            .upper()
        )

        runtime_source = str(
            getattr(
                self.main_window.license_result,
                "source",
                "",
            )
            or ""
        )

        if runtime_source.startswith("ADMIN_USB"):
            self.set_scan_feedback(
                "GOOGLE SHEET VERIFIED — " + sheet_status + " SAVED. USB BETA REMAINS ACTIVE.",
                seconds=12.0,
            )
        else:
            self.set_scan_feedback(
                "GOOGLE SHEET VERIFIED — LICENSE " + sheet_status,
                seconds=12.0,
            )

    @staticmethod
    def format_timestamp(
        value: float | None,
    ) -> str:
        if not value:
            return "Never"
        try:
            return datetime.fromtimestamp(float(value)).strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            return "Unknown"

    @staticmethod
    def format_duration(
        seconds: float,
    ) -> str:
        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(
            total_seconds,
            3600,
        )
        minutes, seconds_part = divmod(
            remainder,
            60,
        )
        return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}"

    @staticmethod
    def masked_machine_id(
        machine_id: str,
    ) -> str:
        value = str(machine_id or "").strip()
        if len(value) <= 4:
            return value or "Unknown"
        return "•" * 12 + value[-4:]

    def refresh_values(self) -> None:
        result = self.main_window.license_result
        status = str(result.status).strip().upper()
        now = time.time()

        snapshot = self.main_window.current_license_countdown_snapshot(now=now)
        valid_until = snapshot.deadline
        remaining = snapshot.remaining_seconds
        countdown_applicable = snapshot.applicable
        grace_hours = float(OFFLINE_GRACE_HOURS)

        last_online = result.last_online_check

        self.status_value.setText(status)
        self.status_value.setProperty(
            "licenseState",
            self.main_window.license_policy.access.style,
        )
        self.status_value.style().unpolish(self.status_value)
        self.status_value.style().polish(self.status_value)

        source = str(
            getattr(
                result,
                "source",
                "SERVER",
            )
            or "SERVER"
        )
        usb_connected = bool(
            getattr(
                result,
                "usb_connected",
                False,
            )
        )
        block_on_exit = bool(
            getattr(
                result,
                "block_on_exit",
                False,
            )
        )

        self.source_value.setText(source)
        self.admin_usb_value.setText(
            "CONNECTED — LICENSE BETA"
            if usb_connected
            else (
                "REMOVED — BETA UNTIL APP CLOSES"
                if source == "ADMIN_USB_SESSION_REMOVED"
                else ("BETA USB SESSION" if source.startswith("ADMIN_USB") else "NOT IN USE")
            )
        )
        self.connection_value.setText("ONLINE" if result.online else "OFFLINE")
        self.last_online_value.setText(self.format_timestamp(last_online))
        self.allowance_value.setText(f"{grace_hours:g} hours")

        if snapshot.mode == "paused_usb":
            self.remaining_value.setText("PAUSED")
            self.expiry_value.setText("BETA while Admin USB is connected")

        elif snapshot.mode == "usb_session":
            self.remaining_value.setText("UNTIL APP CLOSES")
            self.expiry_value.setText("Next launch restores the saved Sheet status")

        elif snapshot.mode == "block_on_exit" or block_on_exit:
            self.remaining_value.setText("00:00:00 · BLOCK ON EXIT")
            self.expiry_value.setText(
                self.format_timestamp(valid_until) if valid_until else "Expired"
            )

        elif snapshot.mode == "online":
            self.remaining_value.setText("—")
            self.expiry_value.setText("—")

        elif snapshot.mode in {
            "countdown",
            "expired",
        }:
            self.remaining_value.setText(self.format_duration(remaining))
            self.expiry_value.setText(
                self.format_timestamp(valid_until) if valid_until else "Not available"
            )

        elif snapshot.mode == "unavailable":
            self.remaining_value.setText("UNAVAILABLE")
            self.expiry_value.setText("No valid offline deadline")

        else:
            self.remaining_value.setText("00:00:00")
            self.expiry_value.setText(
                self.format_timestamp(valid_until) if valid_until else "Not available"
            )

        self.machine_name_value.setText(result.machine_name or "Unknown")
        self.machine_id_value.setText(self.masked_machine_id(result.machine_id))
        default_message = result.message or self.main_window.license_policy.access.message
        if self._scan_feedback_message and time.time() < self._scan_feedback_until:
            self.message_label.setText(self._scan_feedback_message)
        else:
            self._scan_feedback_message = ""
            self._scan_feedback_until = 0.0
            self.message_label.setText(default_message)

        if not countdown_applicable:
            urgency = "inactive"
        elif remaining <= 3600:
            urgency = "critical"
        elif remaining <= float(
            (self.main_window.license_client.usb_admin.warning_seconds)
            if source.startswith("ADMIN_USB")
            else 5 * 3600
        ):
            urgency = "warning"
        elif remaining <= 24 * 3600:
            urgency = "notice"
        else:
            urgency = "normal"

        self.remaining_value.setProperty(
            "urgency",
            urgency,
        )
        self.remaining_value.style().unpolish(self.remaining_value)
        self.remaining_value.style().polish(self.remaining_value)

        if snapshot.expired:
            self.main_window.enforce_local_license_deadline()


class LicenseControllerMixin:
    # ``clear_blocked_cue_runtime`` used to be defined twice in this mixin:
    # this first copy wiped the whole cue table, and a second copy further
    # down replaced it with a no-op because licence restrictions no longer
    # erase cue state. Python bound the second, so the wipe never ran; the
    # dead first copy is gone. The surviving definition is below.

    def license_lock_icon(self) -> QIcon:
        cached = getattr(
            self,
            "_cached_license_lock_icon",
            None,
        )
        if isinstance(cached, QIcon):
            return cached

        pixmap = QPixmap(18, 18)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        lock_color = QColor("#f2c55c")
        painter.setPen(
            QPen(
                lock_color,
                2.2,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(
            QRectF(
                5.0,
                2.0,
                8.0,
                10.0,
            ),
            0,
            180 * 16,
        )

        painter.setPen(
            QPen(
                lock_color,
                1.2,
            )
        )
        painter.setBrush(lock_color)
        painter.drawRoundedRect(
            QRectF(
                3.5,
                8.0,
                11.0,
                8.0,
            ),
            2.0,
            2.0,
        )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2a2114"))
        painter.drawEllipse(
            QRectF(
                8.0,
                10.2,
                2.0,
                2.0,
            )
        )
        painter.drawRoundedRect(
            QRectF(
                8.55,
                11.2,
                0.9,
                2.4,
            ),
            0.4,
            0.4,
        )
        painter.end()

        icon = QIcon(pixmap)
        self._cached_license_lock_icon = icon
        return icon

    def set_license_lock_indicator(
        self,
        button: QPushButton,
        *,
        locked: bool,
        feature_name: str,
        reason: str,
    ) -> None:
        button.setProperty(
            "licenseLocked",
            bool(locked),
        )

        if locked:
            button.setIcon(self.license_lock_icon())
            button.setToolTip(f"{feature_name} bị khóa. {reason}")
            button.setAccessibleDescription(f"{feature_name} locked. {reason}")
        else:
            button.setIcon(self.navigation_icon(feature_name))
            button.setToolTip(f"{feature_name} available")
            button.setAccessibleDescription(f"{feature_name} available")

        button.setIconSize(QSize(20, 20))
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()

    def current_license_countdown_snapshot(
        self,
        *,
        now: float | None = None,
    ) -> LicenseCountdownSnapshot:
        current_time = float(time.time() if now is None else now)

        return resolve_license_countdown(
            self.license_result,
            now=current_time,
            default_grace_hours=float(OFFLINE_GRACE_HOURS),
            usb_runtime_state=getattr(
                self,
                "_usb_runtime_state",
                None,
            ),
        )

    def refresh_license_countdown_display(
        self,
    ) -> None:
        if self._shutdown_in_progress:
            return

        snapshot = self.current_license_countdown_snapshot()

        if snapshot.expired:
            self.enforce_local_license_deadline()

        license_display = self.license_status_display_text()
        self.apply_license_status_caption(license_display)

    def apply_license_status_caption(self, license_display: str) -> None:
        """Label the sidebar's status button.

        The collapsed rail used to blank the button, leaving a bare coloured
        rectangle that told the operator nothing. The colour already carries
        the licence state, so the text can carry the build instead — the first
        thing anyone is asked for when reporting a problem from a venue. Full
        licence wording stays in the tooltip and in the dialog behind the click.
        """
        if bool(getattr(self, "_compact_sidebar", False)):
            self.license_status_button.setText(f"v{tracyy_version}")
            self.license_status_button.setToolTip(
                f"Tracyy {tracyy_version}\nLicense — {license_display}"
            )
        else:
            self.license_status_button.setText(license_display)
            self.license_status_button.setToolTip(f"Tracyy {tracyy_version}")

    def license_status_display_text(self) -> str:
        result = self.license_result
        status = str(result.status).strip().upper()
        snapshot = self.current_license_countdown_snapshot()

        if snapshot.mode == "paused_usb":
            return "BETA · USB"
        if snapshot.mode == "usb_session":
            return "BETA · USB REMOVED"
        if snapshot.mode in {"countdown", "expired"} and status in {
            "ACTIVE",
            "BETA",
            "EXPIRED",
        }:
            remaining = max(0, int(snapshot.remaining_seconds))
            hours, remainder = divmod(remaining, 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"{status} · {hours:02d}:{minutes:02d}:{seconds:02d}"
        return status

    def _apply_usb_runtime_state(self, state) -> None:
        current = self.license_result
        self._usb_runtime_state = state
        source = "ADMIN_USB_CONNECTED" if state.connected else "ADMIN_USB_SESSION_REMOVED"
        self.license_result = LicenseResult(
            status="BETA",
            message=state.message,
            machine_id=current.machine_id,
            machine_name=current.machine_name,
            online=bool(current.online),
            checked_at=time.time(),
            expires_at=None,
            last_online_check=current.last_online_check,
            offline_valid_until=current.offline_valid_until,
            offline_grace_hours=float(OFFLINE_GRACE_HOURS),
            source=source,
            usb_connected=state.connected,
            usb_token_id=state.token_id,
            block_on_exit=False,
        )
        self.apply_license_state()

    def request_usb_license_scan(
        self,
    ) -> bool:
        """
        Force a new local USB scan.

        Google Sheet is not queried. The current saved Sheet state remains
        unchanged unless a valid USB grants the temporary BETA session.
        """
        if (
            self._shutdown_in_progress
            or self._usb_license_scan_in_progress
            or self._google_sheet_scan_in_progress
        ):
            return False

        self._usb_license_scan_in_progress = True
        self._usb_admin_timer.stop()

        self.set_status("SCANNING USB LICENSE…")

        threading.Thread(
            target=self._usb_license_scan_worker,
            name="TracyyUSBLicenseScan",
            daemon=True,
        ).start()

        return True

    def _usb_license_scan_worker(
        self,
    ) -> None:
        state = None
        error = ""

        try:
            state = self.license_client.usb_admin.force_rescan()
        except Exception as exc:
            error = str(exc).strip() or type(exc).__name__

        self.usb_license_scan_finished.emit(
            state,
            error,
        )

    def apply_manual_usb_license_scan_result(
        self,
        state: object,
        error: str,
    ) -> None:
        self._usb_license_scan_in_progress = False

        if not self._shutdown_in_progress and not self._usb_admin_timer.isActive():
            self._usb_admin_timer.start()

        if self._shutdown_in_progress:
            return

        if error:
            self.set_status("USB LICENSE SCAN FAILED — " + error)
            return

        if state is None:
            self.set_status("USB LICENSE SCAN FAILED")
            return

        self._usb_runtime_state = state

        if bool(
            getattr(
                state,
                "active",
                False,
            )
        ):
            self._apply_usb_runtime_state(state)

            if bool(
                getattr(
                    state,
                    "connected",
                    False,
                )
            ):
                self.set_status("USB LICENSE VERIFIED — LICENSE BETA")
            else:
                self.set_status(
                    str(
                        getattr(
                            state,
                            "message",
                            "",
                        )
                        or ("ADMIN USB REMOVED — BETA REMAINS UNTIL APP CLOSES")
                    )
                )
            return

        # Invalid or missing USB must not overwrite the startup/saved
        # Sheet license status.
        license_display = self.license_status_display_text()
        self.apply_license_status_caption(license_display)
        self.set_status(
            str(
                getattr(
                    state,
                    "message",
                    "",
                )
                or "NO VALID USB LICENSE FOUND"
            )
        )

    def request_google_sheet_license_scan(
        self,
    ) -> bool:
        """
        Query Google Sheet in a worker thread.

        A valid Admin USB remains the active BETA override. The Sheet
        result is still saved for the next launch without USB.
        """
        if (
            self._shutdown_in_progress
            or self._google_sheet_scan_in_progress
            or self._usb_license_scan_in_progress
        ):
            return False

        self._google_sheet_scan_in_progress = True
        self._license_connectivity_timer.stop()

        self.set_status("SCANNING GOOGLE SHEET LICENSE…")

        threading.Thread(
            target=self._google_sheet_scan_worker,
            name="TracyyGoogleSheetLicenseScan",
            daemon=True,
        ).start()

        return True

    def _google_sheet_scan_worker(
        self,
    ) -> None:
        result = None
        error = ""

        try:
            result = self.license_client.scan_google_sheet()
        except Exception as exc:
            error = str(exc).strip() or type(exc).__name__

        self.google_sheet_scan_finished.emit(
            result,
            error,
        )

    def apply_manual_google_sheet_scan_result(
        self,
        result: object,
        error: str,
    ) -> None:
        self._google_sheet_scan_in_progress = False

        if not self._shutdown_in_progress and not self._license_connectivity_timer.isActive():
            self._license_connectivity_timer.start()

        if self._shutdown_in_progress:
            return

        if error:
            self.set_status("GOOGLE SHEET LICENSE SCAN FAILED — " + error)
            return

        if not isinstance(
            result,
            LicenseResult,
        ):
            self.set_status("GOOGLE SHEET LICENSE SCAN FAILED — INVALID RESULT")
            return

        self._startup_license_status = str(result.status).strip().upper()
        self._startup_license_message = str(result.message or "")
        self._license_blocked_by_offline_timeout = False

        usb_state = self._usb_runtime_state
        self.license_result = result

        if bool(
            getattr(
                usb_state,
                "active",
                False,
            )
        ):
            self._apply_usb_runtime_state(usb_state)
            self.set_status(
                "GOOGLE SHEET VERIFIED — "
                + str(result.status).strip().upper()
                + " SAVED — USB BETA REMAINS ACTIVE"
            )
        else:
            type(self.license_client)._startup_result = result
            self.apply_license_state()
            self.set_status(
                "GOOGLE SHEET VERIFIED — LICENSE " + str(result.status).strip().upper()
            )

    def update_usb_admin_runtime(self) -> None:
        if self._shutdown_in_progress:
            return

        current = self.license_result
        source = str(getattr(current, "source", "") or "")
        state = self.license_client.usb_admin.poll()
        self._usb_runtime_state = state

        if state.active:
            changed = (
                not source.startswith("ADMIN_USB")
                or bool(getattr(current, "usb_connected", False)) != state.connected
                or getattr(current, "usb_token_id", "") != state.token_id
                or (state.connected and source != "ADMIN_USB_CONNECTED")
                or (not state.connected and source != "ADMIN_USB_SESSION_REMOVED")
            )
            if changed:
                self._apply_usb_runtime_state(state)
            else:
                self.license_status_button.setText(self.license_status_display_text())

            if state.connected:
                self.set_status("ADMIN USB VERIFIED — LICENSE BETA")
            else:
                self.set_status(
                    "ADMIN USB REMOVED — BETA REMAINS UNTIL APP CLOSES; "
                    "NEXT LAUNCH RESTORES SHEET STATUS"
                )
            return

        # No USB session: do not change the Sheet/server/cache state.
        self.license_status_button.setText(self.license_status_display_text())

    def request_license_connectivity_check(
        self,
    ) -> None:
        if self._shutdown_in_progress or self._license_connectivity_check_in_progress:
            return

        self._license_connectivity_check_in_progress = True
        threading.Thread(
            target=self._license_connectivity_worker,
            name="TracyyLicenseConnectivity",
            daemon=True,
        ).start()

    def _license_connectivity_worker(
        self,
    ) -> None:
        reachable = self.license_client.server_reachable(timeout=2.0)
        self.license_connectivity_checked.emit(bool(reachable))

    def apply_license_connectivity(
        self,
        online: bool,
    ) -> None:
        self._license_connectivity_check_in_progress = False

        if self._shutdown_in_progress:
            return

        current = self.license_result
        current_source = str(getattr(current, "source", "") or "")
        if current_source.startswith("ADMIN_USB"):
            # A network reachability probe is not a Sheet license verification.
            # Keep USB sessions in offline mode until the next real startup check.
            self.set_status(
                "NETWORK AVAILABLE — ADMIN USB SESSION REMAINS ACTIVE"
                if online
                else "NETWORK OFFLINE — ADMIN USB SESSION ACTIVE"
            )
            return

        was_online = bool(current.online)
        is_online = bool(online)

        if was_online == is_online and not (
            is_online and self._license_blocked_by_offline_timeout
        ):
            return

        status = str(current.status).strip().upper()
        message = str(current.message or "")

        if (
            is_online
            and self._license_blocked_by_offline_timeout
            and self._startup_license_status in {"ACTIVE", "BETA", "EXPIRED"}
        ):
            status = self._startup_license_status
            message = self._startup_license_message
            self._license_blocked_by_offline_timeout = False

        self.license_result = LicenseResult(
            status=status,
            message=message,
            machine_id=current.machine_id,
            machine_name=current.machine_name,
            online=is_online,
            checked_at=current.checked_at,
            expires_at=current.expires_at,
            last_online_check=(current.last_online_check),
            offline_valid_until=(current.offline_valid_until),
            offline_grace_hours=(current.offline_grace_hours),
            source=getattr(current, "source", "SERVER"),
            usb_connected=getattr(current, "usb_connected", False),
            usb_token_id=getattr(current, "usb_token_id", ""),
            block_on_exit=getattr(current, "block_on_exit", False),
        )
        self.apply_license_state()

        self.set_status("LICENSE CONNECTION — " + ("ONLINE" if is_online else "OFFLINE"))

    def show_license_information(
        self,
    ) -> None:
        dialog = LicenseInformationDialog(self)
        dialog.exec()

    def schedule_license_expiry_timer(
        self,
    ) -> None:
        self._license_expiry_timer.stop()

        snapshot = self.current_license_countdown_snapshot()

        if snapshot.expired:
            QTimer.singleShot(
                0,
                self.enforce_local_license_deadline,
            )
            return

        if snapshot.mode != "countdown" or not snapshot.deadline:
            return

        delay_ms = max(
            1,
            min(
                int(snapshot.remaining_seconds * 1000.0),
                2_147_000_000,
            ),
        )

        self._license_expiry_timer.start(delay_ms)

    def enforce_local_license_deadline(
        self,
    ) -> None:
        result = self.license_result
        status = str(result.status).strip().upper()

        snapshot = self.current_license_countdown_snapshot()

        if snapshot.mode in {
            "online",
            "paused_usb",
            "block_on_exit",
            "inactive",
            "unavailable",
        } or status not in {
            "ACTIVE",
            "BETA",
            "EXPIRED",
        }:
            return

        if not snapshot.expired:
            self.schedule_license_expiry_timer()
            return

        now = time.time()
        valid_until = snapshot.deadline
        source = str(
            getattr(
                result,
                "source",
                "",
            )
            or ""
        )

        if source.startswith("ADMIN_USB"):
            self._usb_runtime_state = self.license_client.usb_admin.poll(now)

            state = self._usb_runtime_state

            if state.connected:
                self._apply_usb_runtime_state(state)
                return

            self.license_result = LicenseResult(
                status="ACTIVE",
                message=state.message,
                machine_id=result.machine_id,
                machine_name=result.machine_name,
                online=False,
                checked_at=now,
                expires_at=valid_until,
                last_online_check=(result.last_online_check),
                offline_valid_until=(valid_until),
                offline_grace_hours=float(OFFLINE_GRACE_HOURS),
                source=("ADMIN_USB_OFFLINE"),
                usb_connected=False,
                usb_token_id=getattr(
                    result,
                    "usb_token_id",
                    "",
                ),
                block_on_exit=True,
            )
            self.apply_license_state()
            self.set_status("USB OFFLINE LICENSE EXPIRED — TRACYY WILL BLOCK WHEN CLOSED")
            return

        grace_hours = float(OFFLINE_GRACE_HOURS)
        self._license_blocked_by_offline_timeout = True
        self.license_result = LicenseResult(
            status="BLOCKED",
            message=(
                f"License has not been verified online within the last {grace_hours:g} hours."
            ),
            machine_id=result.machine_id,
            machine_name=result.machine_name,
            online=False,
            checked_at=now,
            expires_at=valid_until,
            last_online_check=(result.last_online_check),
            offline_valid_until=(valid_until),
            offline_grace_hours=(grace_hours),
            source=source or "SERVER_CACHE",
        )
        self.apply_license_state()

    def update_license_lock_indicators(
        self,
        access: object,
    ) -> None:
        status = str(self.license_policy.status).strip().upper()

        server_locked = not bool(
            getattr(
                access,
                "server",
                False,
            )
        )

        if server_locked:
            server_reason = (
                f"License status: {status}. Server requires BETA, ACTIVE or EXPIRED access."
            )
        else:
            server_reason = ""

        self.set_license_lock_indicator(
            self.nav_server,
            locked=server_locked,
            feature_name="Server",
            reason=server_reason,
        )

    def apply_license_state(self) -> None:
        self.license_policy = LicensePolicy(self.license_result.status)
        policy = self.license_policy
        access = policy.access

        self._cue_runtime_enabled = True
        self._cue_countdown_enabled = bool(access.cue_countdown)

        license_display = self.license_status_display_text()
        self.apply_license_status_caption(license_display)
        self.license_status_button.setToolTip("Open License Information")
        display_style = access.style
        result_source = str(
            getattr(
                self.license_result,
                "source",
                "",
            )
            or ""
        )
        countdown_snapshot = self.current_license_countdown_snapshot()
        if result_source.startswith("ADMIN_USB"):
            if countdown_snapshot.mode == "block_on_exit":
                display_style = "blocked"
            elif countdown_snapshot.mode in {
                "countdown",
                "expired",
            } and countdown_snapshot.remaining_seconds <= float(
                self.license_client.usb_admin.warning_seconds
            ):
                display_style = "expired"

        self.license_status_label.setProperty(
            "licenseState",
            display_style,
        )
        self.license_status_label.style().unpolish(self.license_status_label)
        self.license_status_label.style().polish(self.license_status_label)
        self.schedule_license_expiry_timer()

        self.modules.apply_license(
            self,
            policy,
        )
        self.update_license_lock_indicators(access)

        if self._cue_countdown_enabled:
            self.update_next_cue_loading(self.transport.current_position())
        else:
            self.clear_inline_cue_loading()

    def license_allows_operation(self) -> bool:
        return self.license_policy.access.file

    def clear_blocked_cue_runtime(self) -> None:
        # Kept for compatibility with old callers. License restrictions no
        # longer erase cue selection or cue table state.
        self.clear_inline_cue_loading()
