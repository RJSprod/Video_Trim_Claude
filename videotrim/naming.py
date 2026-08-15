"""Output filenames, so a clip saved from the browser is named like a clip
saved from the desktop window. Qt-free: both front ends import this."""

from pathlib import Path

from .timefmt import fmt_time_filename
from .paths import sanitize


def frame_name(source_path, position_ms):
    stem = sanitize(Path(source_path).stem) if source_path else "frame"
    return f"{stem}_frame_{fmt_time_filename(position_ms)}.png"


def clip_name(source_path, a_ms, b_ms):
    stem = sanitize(Path(source_path).stem) if source_path else "video"
    return (
        f"{stem}_clip_{fmt_time_filename(a_ms)}"
        f"_to_{fmt_time_filename(b_ms)}.mp4"
    )
