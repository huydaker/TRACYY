"""Small Qt helpers shared across pages.

Path resolution moved to :mod:`tracyy.core.paths` in V2; the
``tracyy_*_root`` names are kept as thin aliases so ported controllers keep
working unchanged.
"""

from __future__ import annotations

import math
import socket
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QLabel, QMessageBox, QSizePolicy, QWidget

from .paths import app_root, data_root, resource_root

__all__ = [
    "AdaptiveWrapLabel",
    "local_lan_address",
    "seconds_to_clock",
    "silent_message_box",
    "tracyy_app_root",
    "tracyy_data_root",
    "tracyy_resource_root",
]


class AdaptiveWrapLabel(QLabel):
    """A centred label that shrinks its font and wraps to two lines when the
    available width becomes too small.

    Fitting the font means measuring the text at every candidate point size,
    which V1 redid on every ``setText`` — including the ones that wrote the
    same string. The chosen size is now cached per (text, width) so a label
    updated at the playhead rate measures once and then only assigns.
    """

    def __init__(
        self,
        text: str = "",
        *,
        base_point_size: int = 12,
        min_point_size: int = 8,
        max_lines: int = 2,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self._base_point_size = max(1, int(base_point_size))
        self._min_point_size = max(1, int(min_point_size))
        self._max_lines = max(1, int(max_lines))
        self._fit_cache: dict[tuple[str, int], tuple[int, int]] = {}
        self._last_fit: tuple[str, int] | None = None
        self.setWordWrap(True)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self._apply_font_fit()

    def setText(self, text: str) -> None:
        if text == self.text():
            return
        super().setText(text)
        self._apply_font_fit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_font_fit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_font_fit()

    def _measure(self, text: str, available_width: int) -> tuple[int, int]:
        flags = (
            int(Qt.TextFlag.TextWordWrap)
            | int(Qt.AlignmentFlag.AlignHCenter)
            | int(Qt.AlignmentFlag.AlignVCenter)
        )
        for size in range(self._base_point_size, self._min_point_size - 1, -1):
            font = QFont(self.font())
            font.setPointSize(size)
            metrics = QFontMetrics(font)
            rect = metrics.boundingRect(0, 0, available_width, 1000, flags, text)
            line_spacing = max(1, metrics.lineSpacing())
            lines = max(1, math.ceil(rect.height() / line_spacing))
            if lines <= self._max_lines:
                return size, lines
        return self._min_point_size, self._max_lines

    def _apply_font_fit(self) -> None:
        available_width = max(40, self.contentsRect().width() - 4)
        text = self.text().strip() or " "

        # Bucket the width so a one-pixel resize does not invalidate the fit.
        key = (text, available_width // 8)
        if key == self._last_fit:
            return

        fit = self._fit_cache.get(key)
        if fit is None:
            fit = self._measure(text, available_width)
            if len(self._fit_cache) > 512:
                self._fit_cache.clear()
            self._fit_cache[key] = fit
        chosen_size, chosen_lines = fit
        self._last_fit = key

        font = QFont(self.font())
        if font.pointSize() != chosen_size:
            font.setPointSize(chosen_size)
            self.setFont(font)
            metrics = QFontMetrics(font)
        else:
            metrics = QFontMetrics(font)

        height = max(22, metrics.lineSpacing() * chosen_lines + 4)
        if self.minimumHeight() != height:
            self.setMinimumHeight(height)
            self.updateGeometry()


def seconds_to_clock(seconds: float) -> str:
    whole = max(0, int(seconds))
    mm, ss = divmod(whole, 60)
    hh, mm = divmod(mm, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def local_lan_address() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No packet is sent; this only asks the routing table which local
            # interface would be used to reach the public internet.
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def silent_message_box(
    parent: QWidget | None,
    title: str,
    message: str,
    *,
    buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
    default_button: QMessageBox.StandardButton = QMessageBox.StandardButton.NoButton,
) -> QMessageBox.StandardButton:
    """Show a QMessageBox without the platform notification sound."""
    box = QMessageBox(parent)
    box.setWindowTitle(str(title))
    box.setText(str(message))
    box.setIcon(QMessageBox.Icon.NoIcon)
    box.setStandardButtons(buttons)

    if default_button != QMessageBox.StandardButton.NoButton:
        box.setDefaultButton(default_button)

    # Force the Qt-rendered dialog instead of a platform-native alert.
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)

    result = box.exec()
    try:
        return QMessageBox.StandardButton(result)
    except (TypeError, ValueError):
        return QMessageBox.StandardButton.NoButton


def tracyy_resource_root() -> Path:
    return resource_root()


def tracyy_data_root() -> Path:
    return data_root()


def tracyy_app_root() -> Path:
    return app_root()
