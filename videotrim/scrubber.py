"""Seek bar that also renders the A-B region and its markers."""

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import theme

TRACK_H = 6.0
KNOB_R = 8.0
KNOB_R_ACTIVE = 10.5


class Scrubber(QWidget):
    scrubStarted = Signal()
    scrubMoved = Signal(int)  # live position, emitted throughout the drag
    scrubEnded = Signal(int)  # final position
    scrubbing = Signal(bool, int)  # active, preview position (drives the labels)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(theme.SCRUB_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self._duration = 0
        self._position = 0
        self._a = None
        self._b = None
        self._dragging = False
        self._hover = False

    # --- state ---------------------------------------------------------------
    def set_duration(self, ms):
        self._duration = max(0, int(ms))
        self.update()

    def set_position(self, ms):
        if not self._dragging:
            self._position = max(0, int(ms))
            self.update()

    def set_markers(self, a, b):
        self._a, self._b = a, b
        self.update()

    # --- geometry ------------------------------------------------------------
    def _track_rect(self):
        rect = QRectF(self.rect())
        inset = KNOB_R_ACTIVE + 1
        return QRectF(
            rect.left() + inset,
            rect.center().y() - TRACK_H / 2.0,
            max(1.0, rect.width() - inset * 2),
            TRACK_H,
        )

    def _x_for(self, ms):
        track = self._track_rect()
        if self._duration <= 0:
            return track.left()
        ratio = max(0.0, min(1.0, ms / float(self._duration)))
        return track.left() + ratio * track.width()

    def _ms_for(self, x):
        track = self._track_rect()
        if track.width() <= 0 or self._duration <= 0:
            return 0
        ratio = (x - track.left()) / track.width()
        return int(max(0.0, min(1.0, ratio)) * self._duration)

    # --- painting ------------------------------------------------------------
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track = self._track_rect()
        radius = track.height() / 2.0

        painter.setPen(Qt.NoPen)

        # The A-B span sits behind the track and stands proud of it, so the
        # region stays readable once the progress fill covers the middle.
        if self._a is not None and self._b is not None and self._duration > 0:
            left, right = self._x_for(self._a), self._x_for(self._b)
            band = QRectF(left, track.top() - 4.0, max(1.0, right - left), track.height() + 8.0)
            painter.setBrush(theme.AB_FILL)
            painter.drawRoundedRect(band, 4.0, 4.0)

        painter.setBrush(theme.TRACK)
        painter.drawRoundedRect(track, radius, radius)

        played = QRectF(track.left(), track.top(), max(0.0, self._x_for(self._position) - track.left()), track.height())
        painter.setBrush(theme.ACCENT)
        painter.drawRoundedRect(played, radius, radius)

        # Marker pins sit above the track so they survive being overlapped.
        for value in (self._a, self._b):
            if value is None or self._duration <= 0:
                continue
            x = self._x_for(value)
            painter.setPen(QPen(theme.AB_MARK, 2.0))
            painter.drawLine(QPointF(x, track.top() - 5), QPointF(x, track.bottom() + 5))
            painter.setPen(Qt.NoPen)
            painter.setBrush(theme.AB_MARK)
            painter.drawEllipse(QPointF(x, track.top() - 6), 2.6, 2.6)

        knob_r = KNOB_R_ACTIVE if (self._dragging or self._hover) else KNOB_R
        centre = QPointF(self._x_for(self._position), track.center().y())
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.PANEL_TINT.darker(160))
        painter.drawEllipse(centre, knob_r + 1.5, knob_r + 1.5)
        painter.setBrush(theme.TEXT)
        painter.drawEllipse(centre, knob_r, knob_r)
        painter.end()

    # --- interaction ---------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or self._duration <= 0:
            return
        self._dragging = True
        self._position = self._ms_for(event.position().x())
        self.scrubStarted.emit()
        self.scrubbing.emit(True, self._position)
        self.scrubMoved.emit(self._position)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        position = self._ms_for(event.position().x())
        if position != self._position:
            self._position = position
            self.scrubbing.emit(True, position)
            self.scrubMoved.emit(position)
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self._position = self._ms_for(event.position().x())
        self.scrubEnded.emit(self._position)
        self.scrubbing.emit(False, self._position)
        self.update()
        event.accept()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)
