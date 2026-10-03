from __future__ import annotations


def frames_to_timecode(total_frames: int, fps: int) -> str:
    frames_per_100_hours = 100 * 60 * 60 * fps
    total_frames = max(0, int(total_frames)) % frames_per_100_hours

    ff = total_frames % fps
    total_seconds = total_frames // fps
    ss = total_seconds % 60
    total_minutes = total_seconds // 60
    mm = total_minutes % 60
    hh = (total_minutes // 60) % 100
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def timecode_to_frames(value: str, fps: int) -> int:
    parts = str(value or "").strip().split(":")
    if len(parts) != 4:
        raise ValueError("Timecode phải có dạng HH:MM:SS:FF.")
    hh, mm, ss, ff = map(int, parts)
    if not 0 <= hh <= 99:
        raise ValueError("Giờ phải trong khoảng 00–99.")
    if not 0 <= mm < 60 or not 0 <= ss < 60:
        raise ValueError("Phút và giây phải trong khoảng 0–59.")
    if not 0 <= ff < fps:
        raise ValueError(f"Frame phải trong khoảng 0–{fps - 1}.")
    return (((hh * 60) + mm) * 60 + ss) * fps + ff
