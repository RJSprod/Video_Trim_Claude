"""Vector glyphs and the glassy buttons that draw them.

Everything is painted with QPainter so the app ships with no image assets and
stays crisp at any DPI.  Glyph paths are authored in a 100x100 box and scaled
into the button at paint time.
"""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton

from . import theme


def _play():
    path = QPainterPath()
    path.moveTo(26, 16)
    path.lineTo(84, 50)
    path.lineTo(26, 84)
    path.closeSubpath()
    return path


def _pause():
    path = QPainterPath()
    path.addRoundedRect(QRectF(26, 16, 16, 68), 5, 5)
    path.addRoundedRect(QRectF(58, 16, 16, 68), 5, 5)
    return path


def _stop():
    path = QPainterPath()
    path.addRoundedRect(QRectF(22, 22, 56, 56), 9, 9)
    return path


def _chevrons(back):
    """Double chevron used by the 5-second skip buttons."""
    path = QPainterPath()
    for offset in (0, 30):
        tip = QPointF(20 + offset, 50)
        top = QPointF(48 + offset, 24)
        bottom = QPointF(48 + offset, 76)
        if not back:
            tip = QPointF(78 - offset, 50)
            top = QPointF(50 - offset, 24)
            bottom = QPointF(50 - offset, 76)
        sub = QPainterPath()
        sub.moveTo(top)
        sub.lineTo(tip)
        sub.lineTo(bottom)
        path.addPath(_stroke(sub, 13))
    return path


def _stroke(path, width, cap=Qt.RoundCap, join=Qt.RoundJoin):
    from PySide6.QtGui import QPainterPathStroker

    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    stroker.setCapStyle(cap)
    stroker.setJoinStyle(join)
    return stroker.createStroke(path)


def _polygon(points, scale):
    path = QPainterPath()
    path.moveTo(points[0][0] * scale, points[0][1] * scale)
    for x, y in points[1:]:
        path.lineTo(x * scale, y * scale)
    path.closeSubpath()
    return path


def _repeat():
    """Two offset arrows chasing each other -- the familiar repeat mark."""
    scale = 100.0 / 24.0
    upper = [(7, 7), (17, 7), (17, 10), (21, 6), (17, 2), (17, 5), (5, 5), (5, 11), (7, 11)]
    lower = [(17, 17), (7, 17), (7, 14), (3, 18), (7, 22), (7, 19), (19, 19), (19, 13), (17, 13)]
    path = _polygon(upper, scale)
    path.addPath(_polygon(lower, scale))
    return path


def _camera():
    path = QPainterPath()
    body = QPainterPath()
    body.addRoundedRect(QRectF(12, 30, 76, 50), 10, 10)
    bump = QPainterPath()
    bump.addRoundedRect(QRectF(34, 20, 32, 16), 5, 5)
    body = body.united(bump)
    lens = QPainterPath()
    lens.addEllipse(QPointF(50, 56), 17, 17)
    inner = QPainterPath()
    inner.addEllipse(QPointF(50, 56), 9, 9)
    path.addPath(body.subtracted(lens))
    path.addPath(lens.subtracted(inner))
    return path


def _save_clip():
    """Down arrow landing on a tray -- 'export this to disk'."""
    path = QPainterPath()
    shaft = QPainterPath()
    shaft.addRoundedRect(QRectF(43, 14, 14, 34), 5, 5)
    path.addPath(shaft)

    head = QPainterPath()
    head.moveTo(28, 42)
    head.lineTo(72, 42)
    head.lineTo(50, 68)
    head.closeSubpath()
    path.addPath(head)

    tray = QPainterPath()
    tray.moveTo(18, 62)
    tray.lineTo(18, 82)
    tray.lineTo(82, 82)
    tray.lineTo(82, 62)
    path.addPath(_stroke(tray, 11, cap=Qt.FlatCap))
    return path


def _folder():
    path = QPainterPath()
    body = QPainterPath()
    body.moveTo(14, 30)
    body.lineTo(42, 30)
    body.lineTo(50, 40)
    body.lineTo(86, 40)
    body.lineTo(86, 76)
    body.lineTo(14, 76)
    body.closeSubpath()
    inner = QPainterPath()
    inner.moveTo(24, 40)
    inner.lineTo(38, 40)
    inner.lineTo(46, 50)
    inner.lineTo(76, 50)
    inner.lineTo(76, 66)
    inner.lineTo(24, 66)
    inner.closeSubpath()
    path.addPath(body.subtracted(inner))
    return path


def _speaker(muted):
    path = QPainterPath()
    cone = QPainterPath()
    cone.moveTo(16, 38)
    cone.lineTo(32, 38)
    cone.lineTo(52, 20)
    cone.lineTo(52, 80)
    cone.lineTo(32, 62)
    cone.lineTo(16, 62)
    cone.closeSubpath()
    path.addPath(cone)

    if muted:
        cross = QPainterPath()
        cross.moveTo(64, 36)
        cross.lineTo(88, 64)
        cross.moveTo(88, 36)
        cross.lineTo(64, 64)
        path.addPath(_stroke(cross, 9))
    else:
        for radius in (14, 26):
            wave = QPainterPath()
            wave.moveTo(60, 50)
            wave.arcMoveTo(QRectF(60 - radius, 50 - radius, radius * 2, radius * 2), 55)
            wave.arcTo(QRectF(60 - radius, 50 - radius, radius * 2, radius * 2), 55, -110)
            path.addPath(_stroke(wave, 8))
    return path


_GLYPHS = {
    "play": _play,
    "pause": _pause,
    "stop": _stop,
    "rewind": lambda: _chevrons(True),
    "forward": lambda: _chevrons(False),
    "repeat": _repeat,
    "camera": _camera,
    "save": _save_clip,
    "folder": _folder,
    "volume": lambda: _speaker(False),
    "mute": lambda: _speaker(True),
}

_CACHE = {}


def glyph(name):
    if name not in _CACHE:
        _CACHE[name] = _GLYPHS[name]()
    return _CACHE[name]


class GlassButton(QAbstractButton):
    """Flat, translucent, touch sized button that paints a vector glyph.

    ``badge`` renders a small superscript (used for the "5" on the skip
    buttons); ``on`` gives checkable buttons an accent-tinted pill.
    """

    def __init__(self, name, tooltip="", parent=None, primary=False, badge=""):
        super().__init__(parent)
        self._name = name
        self._badge = badge
        self._primary = primary
        self.setToolTip(tooltip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        size = theme.BTN_SIZE_PRIMARY if primary else theme.BTN_SIZE
        self.setFixedSize(size, size)

    def set_glyph(self, name):
        if name != self._name:
            self._name = name
            self.update()

    def sizeHint(self):
        size = theme.BTN_SIZE_PRIMARY if self._primary else theme.BTN_SIZE
        return QSize(size, size)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())

        checked = self.isCheckable() and self.isChecked()
        if self.isDown():
            back = theme.BTN_DOWN
        elif checked:
            back = theme.BTN_ON
        elif self.underMouse():
            back = theme.BTN_HOVER
        elif self._primary:
            back = theme.BTN_HOVER
        else:
            back = None

        if back is not None:
            painter.setPen(Qt.NoPen)
            painter.setBrush(back)
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 13, 13)

        colour = theme.ACCENT if checked else theme.TEXT
        if not self.isEnabled():
            colour = theme.TEXT_FAINT

        icon = 22.0 if not self._primary else 26.0
        painter.save()
        painter.translate(rect.center())
        painter.scale(icon / 100.0, icon / 100.0)
        painter.translate(-50, -50)
        painter.setBrush(colour)
        painter.setPen(Qt.NoPen)
        painter.drawPath(glyph(self._name))
        painter.restore()

        if self._badge:
            font = QFont(self.font())
            font.setPointSizeF(max(7.0, self.font().pointSizeF() - 1.5))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(colour))
            painter.drawText(rect.adjusted(0, 8, 0, 0), Qt.AlignHCenter | Qt.AlignBottom, self._badge)
        painter.end()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)


class GlassTextButton(QAbstractButton):
    """Large labelled button used by the empty state."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    def sizeHint(self):
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self.text()) + 92, 54)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        if self.isDown():
            back = theme.BTN_DOWN
        elif self.underMouse():
            back = theme.BTN_HOVER
        else:
            back = theme.PANEL_TINT
        painter.setPen(Qt.NoPen)
        painter.setBrush(back)
        painter.drawRoundedRect(rect, 16, 16)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(theme.PANEL_EDGE, 1.0))
        painter.drawRoundedRect(rect, 16, 16)

        painter.save()
        painter.translate(rect.left() + 34, rect.center().y())
        painter.scale(0.22, 0.22)
        painter.translate(-50, -50)
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.ACCENT)
        painter.drawPath(glyph("folder"))
        painter.restore()

        font = QFont(self.font())
        font.setBold(True)
        font.setPointSizeF(self.font().pointSizeF() + 1.0)
        painter.setFont(font)
        painter.setPen(QPen(theme.TEXT))
        painter.drawText(rect.adjusted(58, 0, -18, 0), Qt.AlignVCenter | Qt.AlignLeft, self.text())
        painter.end()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)


class ABButton(QAbstractButton):
    """VLC-style A-B marker button: tap for A, again for B, again to clear."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = 0  # 0 = clear, 1 = A set, 2 = A and B set
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(int(theme.BTN_SIZE * 1.32), theme.BTN_SIZE)
        self._sync_tip()

    def set_state(self, state):
        if state != self._state:
            self._state = state
            self._sync_tip()
            self.update()

    def _sync_tip(self):
        tips = {
            0: "Set loop point A  (B)",
            1: "Set loop point B  (B)",
            2: "Clear A-B loop  (B)",
        }
        self.setToolTip(tips[self._state])

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(3, 3, -3, -3)

        if self.isDown():
            back = theme.BTN_DOWN
        elif self._state == 2:
            back = theme.AB_FILL
        elif self.underMouse():
            back = theme.BTN_HOVER
        else:
            back = None

        if back is not None:
            painter.setPen(Qt.NoPen)
            painter.setBrush(back)
            painter.drawRoundedRect(rect, 13, 13)

        if self._state == 2:
            painter.setPen(QPen(theme.AB_MARK, 1.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, 13, 13)

        font = QFont(self.font())
        font.setBold(True)
        font.setPointSizeF(self.font().pointSizeF() + 0.5)
        painter.setFont(font)

        metrics = painter.fontMetrics()
        a_col = theme.AB_MARK if self._state >= 1 else theme.TEXT_DIM
        b_col = theme.AB_MARK if self._state >= 2 else theme.TEXT_DIM
        dash_col = theme.AB_MARK if self._state >= 2 else theme.TEXT_FAINT

        parts = [("A", a_col), ("-", dash_col), ("B", b_col)]
        total = sum(metrics.horizontalAdvance(text) for text, _ in parts)
        x = rect.center().x() - total / 2.0
        baseline = rect.center().y() + metrics.ascent() / 2.0 - 1
        for text, colour in parts:
            painter.setPen(QPen(colour))
            painter.drawText(QPointF(x, baseline), text)
            x += metrics.horizontalAdvance(text)
        painter.end()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)
