"""Colours, metrics and small formatting helpers shared by the UI."""

from PySide6.QtGui import QColor

# --- palette -----------------------------------------------------------------
BG = QColor(9, 10, 13)

ACCENT = QColor(79, 195, 247)
ACCENT_SOFT = QColor(79, 195, 247, 70)

AB_MARK = QColor(255, 183, 77)
AB_FILL = QColor(255, 183, 77, 96)

TEXT = QColor(236, 240, 246)
TEXT_DIM = QColor(236, 240, 246, 145)
TEXT_FAINT = QColor(236, 240, 246, 95)

PANEL_TINT = QColor(13, 15, 21, 168)
PANEL_EDGE = QColor(255, 255, 255, 36)

BTN_HOVER = QColor(255, 255, 255, 30)
BTN_DOWN = QColor(255, 255, 255, 62)
BTN_ON = QColor(79, 195, 247, 66)

TRACK = QColor(255, 255, 255, 50)
TRACK_BUFFER = QColor(255, 255, 255, 28)

# --- metrics -----------------------------------------------------------------
PANEL_RADIUS = 18
PANEL_MARGIN = 14  # gap between the frosted panel and the window edge
BTN_SIZE = 44  # touch friendly hit target
BTN_SIZE_PRIMARY = 54
SCRUB_HEIGHT = 34

# Windows narrower than this start clipping the control row. The bar needs
# ~710px for its buttons and labels, plus the panel margin on both sides.
MIN_WINDOW = (780, 470)


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
