"""Time formatting shared by every front end. Deliberately free of Qt imports."""


def fmt_time(ms, tenths=False):
    """Format milliseconds as ``m:ss`` / ``h:mm:ss`` for on-screen display."""
    ms = max(0, int(ms or 0))
    secs, rem = divmod(ms, 1000)
    mins, secs = divmod(secs, 60)
    hours, mins = divmod(mins, 60)
    base = f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"
    return f"{base}.{rem // 100}" if tenths else base


def fmt_time_filename(ms):
    """Format milliseconds for use inside a filename (no illegal characters)."""
    ms = max(0, int(ms or 0))
    secs, rem = divmod(ms, 1000)
    mins, secs = divmod(secs, 60)
    hours, mins = divmod(mins, 60)
    stem = f"{mins:02d}m{secs:02d}.{rem // 100}s"
    return f"{hours}h{stem}" if hours else stem
