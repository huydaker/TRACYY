from __future__ import annotations

import multiprocessing as mp
import os
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path

from .media_tasks import analyze_waveform


class MediaWorkerPool:
    """Lazy spawned-process pool for non-realtime waveform analysis.

    V1.2.3 keeps playback decode to the bounded StreamingAudioSource decoder
    process. This pool therefore never hands full-song PCM back to the GUI.
    """

    def __init__(self, max_workers: int | None = None) -> None:
        cpu_count = max(2, int(os.cpu_count() or 2))
        default_workers = max(2, min(4, cpu_count - 2))
        env_workers = str(os.environ.get("TRACYY_MEDIA_WORKERS", "")).strip()
        if max_workers is None and env_workers:
            try:
                max_workers = int(env_workers)
            except Exception:
                max_workers = None
        self.max_workers = max(1, min(6, int(max_workers or default_workers)))
        self._executor: ProcessPoolExecutor | None = None
        self._lock = threading.RLock()
        self._closed = False

    def _ensure_executor(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._closed:
                raise RuntimeError("Media worker pool đã đóng")
            if self._executor is None:
                context = mp.get_context("spawn")
                self._executor = ProcessPoolExecutor(
                    max_workers=self.max_workers,
                    mp_context=context,
                )
            return self._executor

    def submit_waveform(
        self,
        path: str,
        points: int = 65536,
    ) -> Future:
        return self._ensure_executor().submit(
            analyze_waveform,
            str(Path(path).resolve()),
            int(points),
        )

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            executor = self._executor
            self._executor = None

        if executor is not None:
            executor.shutdown(wait=bool(wait), cancel_futures=True)
