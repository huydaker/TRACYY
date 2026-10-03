"""Tracyy audio package.

Kept import-light on purpose: spawned decoder processes import
``tracyy.audio.streaming`` and must not drag in Qt, sounddevice or the GUI.
``Transport`` is therefore resolved lazily.
"""

from __future__ import annotations

__all__ = [
    "LTCGenerator",
    "PcmRingBuffer",
    "StreamResampler",
    "StreamingAudioSource",
    "Transport",
    "decode_audio",
    "ltc_frame_bits",
    "make_waveform_preview",
    "probe_audio",
    "resampler_name",
]

_LAZY = {
    "Transport": ("tracyy.audio.transport", "Transport"),
    "decode_audio": ("tracyy.audio.transport", "decode_audio"),
    "make_waveform_preview": ("tracyy.audio.transport", "make_waveform_preview"),
    "LTCGenerator": ("tracyy.audio.ltc_encoder", "LTCGenerator"),
    "ltc_frame_bits": ("tracyy.audio.ltc_encoder", "ltc_frame_bits"),
    "PcmRingBuffer": ("tracyy.audio.ringbuffer", "PcmRingBuffer"),
    "StreamResampler": ("tracyy.audio.resampler", "StreamResampler"),
    "resampler_name": ("tracyy.audio.resampler", "resampler_name"),
    "StreamingAudioSource": ("tracyy.audio.streaming", "StreamingAudioSource"),
    "probe_audio": ("tracyy.audio.streaming", "probe_audio"),
}


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    import importlib

    return getattr(importlib.import_module(module_name), attribute)


def __dir__() -> list[str]:
    return sorted(__all__)
