from __future__ import annotations

import math

import numpy as np


def _reduce_pairwise(level: np.ndarray) -> np.ndarray:
    """Reduce one amplitude waveform level while preserving transient peaks."""
    data = np.asarray(level, dtype=np.float32)
    if data.ndim != 2 or data.shape[1] < 1 or len(data) <= 1:
        return np.ascontiguousarray(data, dtype=np.float32)

    if len(data) & 1:
        data = np.vstack((data, data[-1:]))

    pairs = data.reshape(-1, 2, data.shape[1])
    # Peak pooling keeps kick/transient geometry visible when zooming out.
    result = np.max(pairs, axis=1)
    return np.ascontiguousarray(result, dtype=np.float32)


def build_waveform_pyramid(
    preview: np.ndarray,
    min_points: int = 64,
) -> tuple[np.ndarray, ...]:
    """Build a cheap multi-resolution amplitude pyramid without FFT work."""
    base = np.asarray(preview, dtype=np.float32)
    if base.ndim == 1:
        base = base.reshape(-1, 1)
    if base.ndim != 2 or base.shape[1] < 1:
        raise ValueError("preview must have shape (N, >=1)")

    levels: list[np.ndarray] = [np.ascontiguousarray(base)]
    minimum = max(16, int(min_points))

    while len(levels[-1]) > minimum:
        reduced = _reduce_pairwise(levels[-1])
        if len(reduced) >= len(levels[-1]):
            break
        levels.append(reduced)

    return tuple(levels)


def select_lod_level(
    levels: tuple[np.ndarray, ...],
    view_span: float,
    screen_width: float,
    target_points_per_pixel: float = 1.35,
) -> int:
    """Choose the cheapest LOD that still has enough visible detail."""
    if not levels:
        return 0

    span = max(1.0e-6, min(1.0, float(view_span)))
    width = max(1.0, float(screen_width))
    base_visible = max(1.0, len(levels[0]) * span)
    desired_reduction = base_visible / (width * max(0.5, target_points_per_pixel))

    if desired_reduction <= 1.0:
        return 0

    level = math.floor(math.log2(desired_reduction))
    return max(0, min(len(levels) - 1, level))
