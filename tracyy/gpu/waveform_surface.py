from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

GPU_AVAILABLE = False
GPU_BACKEND_NAME = "software"

try:
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
    from PySide6.QtQuickWidgets import QQuickWidget

    if os.environ.get("TRACYY_GPU_TIMELINE", "1") != "0":
        if sys.platform == "darwin":
            try:
                QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Metal)
                GPU_BACKEND_NAME = "Metal / Qt Quick"
            except Exception:
                GPU_BACKEND_NAME = "Qt Quick (automatic backend)"
        else:
            GPU_BACKEND_NAME = "Qt Quick (automatic backend)"
        GPU_AVAILABLE = True
except Exception:
    QQuickWidget = None  # type: ignore[assignment]


class GpuWaveformSurface(QQuickWidget if GPU_AVAILABLE else object):
    """Qt Quick scene-graph waveform/timeline surface.

    On macOS Qt Quick is explicitly requested to use Metal. The waveform bars,
    played-area mask, cue lines and playhead are scene-graph items; they are not
    rasterized into QPixmap tiles by the QWidget paint loop.
    """

    def __init__(self, parent=None) -> None:
        if not GPU_AVAILABLE:
            raise RuntimeError("Qt Quick GPU surface không khả dụng")
        super().__init__(parent)
        self.setObjectName("tracyyGpuWaveformSurface")
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        qml_path = Path(__file__).with_name("waveform_surface.qml")
        self.setSource(QUrl.fromLocalFile(str(qml_path)))
        self._root = self.rootObject()
        if self._root is None:
            errors = "; ".join(str(error) for error in self.errors())
            raise RuntimeError(f"Không tạo được GPU waveform QML: {errors}")

        self._static_key: tuple | None = None
        self._cue_key: tuple | None = None

    @property
    def backend_name(self) -> str:
        return GPU_BACKEND_NAME

    def set_amplitudes(self, amplitudes: np.ndarray, key: tuple) -> None:
        if self._root is None or key == self._static_key:
            return
        values = np.asarray(amplitudes, dtype=np.float32).reshape(-1)
        self._root.setProperty("amplitudes", values.tolist())
        self._static_key = key

    def set_cues(self, cues: list[dict[str, object]], key: tuple) -> None:
        if self._root is None or key == self._cue_key:
            return
        self._root.setProperty("cueLines", cues)
        self._cue_key = key

    def set_progress_ratio(self, ratio: float) -> None:
        if self._root is None:
            return
        value = float(ratio)
        self._root.setProperty("playedRatio", max(0.0, min(1.0, value)))
        self._root.setProperty(
            "playheadRatio",
            value if 0.0 <= value <= 1.0 else -1.0,
        )

    def invalidate_static(self) -> None:
        self._static_key = None
        self._cue_key = None
