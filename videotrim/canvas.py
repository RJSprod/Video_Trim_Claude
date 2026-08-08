"""The video surface: frame painting, frosted glass, gestures and toasts.

Frames are painted by this widget rather than handed to a QVideoWidget.  That
costs a little CPU but buys two things the overlay design depends on: the
control bar can genuinely blur and blend with the live video behind it, and a
full-resolution copy of the displayed frame is always on hand for screenshots.
"""

from PySide6.QtCore import (
    Property,
    QDateTime,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel, QWidget

from . import icons, theme
from .controls import ControlBar

AUTO_HIDE_MS = 3000
FLASH_MS = 520
TAP_SLOP = 14  # px of movement still counted as a tap


class Toast(QLabel):
    """Small transient message that fades in above the control bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            "background: rgba(13,15,21,220);"
            " color: rgba(236,240,246,255);"
            " border: 1px solid rgba(255,255,255,40);"
            " border-radius: 14px;"
            " padding: 9px 18px;"
            " font-weight: 600;"
        )
        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(0.0)
        self.setGraphicsEffect(self._fx)
        self._anim = QPropertyAnimation(self._fx, b"opacity", self)
        self._anim.setDuration(180)
        self._dismiss = QTimer(self)
        self._dismiss.setSingleShot(True)
        self._dismiss.timeout.connect(self._fade_out)
        self.hide()

    def show_message(self, text, msec=2400):
        """Show ``text``; ``msec <= 0`` keeps it up until dismissed explicitly."""
        if not text:
            return
        self.setText(text)
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self._fx.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._dismiss.stop()
        if msec > 0:
            self._dismiss.start(msec)

    def dismiss(self):
        if self.isVisible():
            self._dismiss.stop()
            self._fade_out()

    def _fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(self._fx.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()
        QTimer.singleShot(200, self.hide)

    def _reposition(self):
        parent = self.parentWidget()
        if not parent:
            return
        x = (parent.width() - self.width()) // 2
        y = parent.height() - self.height() - 150
        self.move(max(8, x), max(8, y))


class VideoCanvas(QWidget):
    openRequested = Signal()

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self._player = player
        self.setMinimumSize(*theme.MIN_WINDOW)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._frame = None
        self._image = None
        self._serial = 0
        self._blur_cache = None

        self.controls = ControlBar(self)
        self._controls_fx = QGraphicsOpacityEffect(self.controls)
        self._controls_fx.setOpacity(1.0)
        self.controls.setGraphicsEffect(self._controls_fx)
        self._overlay = 1.0
        self._controls_shown = True

        self.toast = Toast(self)

        self.empty_button = icons.GlassTextButton("Open a video…", self)
        self.empty_button.clicked.connect(self.openRequested)
        self.empty_hint = QLabel(
            "or drop a file here    ·    Ctrl+O", self
        )
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.empty_hint.setStyleSheet("color: rgba(236,240,246,120); background: transparent;")

        self._fade = QPropertyAnimation(self, b"overlayOpacity", self)
        self._fade.setDuration(190)
        self._fade.setEasingCurve(QEasingCurve.InOutQuad)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_controls)

        # Single tap must wait to see whether a second tap turns it into a
        # double tap, so its action is deferred by the double-click interval.
        self._tap_timer = QTimer(self)
        self._tap_timer.setSingleShot(True)
        self._tap_timer.timeout.connect(self._commit_single_tap)
        self._press_pos = None
        self._suppress_reveal_until = 0

        self._flash = None
        self._flash_anim = QVariantAnimation(self)
        self._flash_anim.setDuration(FLASH_MS)
        self._flash_anim.setStartValue(0.0)
        self._flash_anim.setEndValue(1.0)
        self._flash_anim.valueChanged.connect(lambda _v: self.update())
        self._flash_anim.finished.connect(self._clear_flash)

        player.sink.videoFrameChanged.connect(self._on_frame)
        player.playingChanged.connect(self._on_playing)

        self._show_empty_state(True)

    # --- overlay opacity property -------------------------------------------
    def _get_overlay(self):
        return self._overlay

    def _set_overlay(self, value):
        self._overlay = float(value)
        self._controls_fx.setOpacity(self._overlay)
        self.update()

    overlayOpacity = Property(float, _get_overlay, _set_overlay)

    # --- media --------------------------------------------------------------
    def clear_frame(self):
        self._frame = None
        self._image = None
        self._blur_cache = None
        self.update()

    def current_image(self):
        """Detached full-resolution copy of the frame currently on screen."""
        if self._image is None or self._image.isNull():
            return None
        return self._image.copy()

    def _on_frame(self, frame):
        if frame is None or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        # Hold the frame so the buffer backing `image` stays alive.
        self._frame = frame
        self._image = image
        self._serial += 1
        self._blur_cache = None
        self.update()

    def _on_playing(self, playing):
        if playing:
            self._restart_hide_timer()
        else:
            # Paused is when you mark loops and grab stills -- keep the UI up.
            self._hide_timer.stop()
            self.show_controls()

    def _show_empty_state(self, visible):
        self.empty_button.setVisible(visible)
        self.empty_hint.setVisible(visible)
        self.controls.setVisible(not visible)
        if not visible:
            self.show_controls()
        self._layout_children()

    def set_media_loaded(self, loaded):
        self._show_empty_state(not loaded)
        self.controls.set_media_loaded(loaded)

    # --- layout -------------------------------------------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._blur_cache = None
        self._layout_children()

    def _layout_children(self):
        margin = theme.PANEL_MARGIN
        height = self.controls.sizeHint().height()
        self.controls.setGeometry(
            margin,
            max(margin, self.height() - height - margin),
            max(80, self.width() - margin * 2),
            height,
        )
        button_size = self.empty_button.sizeHint()
        self.empty_button.setGeometry(
            (self.width() - button_size.width()) // 2,
            (self.height() - button_size.height()) // 2 - 14,
            button_size.width(),
            button_size.height(),
        )
        self.empty_hint.setGeometry(0, self.empty_button.geometry().bottom() + 14, self.width(), 24)
        self.toast._reposition()

    def _panel_rect(self):
        return QRectF(self.controls.geometry())

    def _video_rect(self):
        if self._image is None or self._image.isNull():
            return QRectF()
        iw, ih = self._image.width(), self._image.height()
        if iw <= 0 or ih <= 0:
            return QRectF()
        scale = min(self.width() / float(iw), self.height() / float(ih))
        vw, vh = iw * scale, ih * scale
        return QRectF((self.width() - vw) / 2.0, (self.height() - vh) / 2.0, vw, vh)

    # --- painting -----------------------------------------------------------
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.BG)

        if self._image is not None and not self._image.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawImage(self._video_rect(), self._image)
        else:
            self._paint_empty_backdrop(painter)

        if self.controls.isVisible() and self._overlay > 0.01:
            painter.setRenderHint(QPainter.Antialiasing, True)
            self._paint_frosted(painter, self._panel_rect(), self._overlay)

        if self._flash is not None:
            painter.setRenderHint(QPainter.Antialiasing, True)
            self._paint_flash(painter)
        painter.end()

    def _paint_empty_backdrop(self, painter):
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(22, 25, 33))
        gradient.setColorAt(1.0, QColor(9, 10, 13))
        painter.fillRect(self.rect(), gradient)

    def _blurred_backdrop(self, rect):
        """Cheap frosted-glass source: downscale the region, then scale it back."""
        if self._image is None or self._image.isNull():
            return None
        video = self._video_rect()
        covered = rect.intersected(video)
        if covered.width() < 2 or covered.height() < 2:
            return None

        key = (self._serial, covered.toRect().getRect())
        if self._blur_cache and self._blur_cache[0] == key:
            return covered, self._blur_cache[1]

        sx = self._image.width() / video.width()
        sy = self._image.height() / video.height()
        source = QRectF(
            (covered.left() - video.left()) * sx,
            (covered.top() - video.top()) * sy,
            covered.width() * sx,
            covered.height() * sy,
        ).intersected(QRectF(0, 0, self._image.width(), self._image.height()))
        if source.width() < 2 or source.height() < 2:
            return None

        # Collapse the region to a thumbnail, then grow it back in two smooth
        # steps. Cheaper than a real gaussian and, at this radius, reads the
        # same once the tint goes over it.
        patch = self._image.copy(source.toRect())
        tiny_w = max(2, int(covered.width() / 22))
        tiny_h = max(2, int(covered.height() / 22))
        blurred = patch.scaled(tiny_w, tiny_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        blurred = blurred.scaled(tiny_w * 4, tiny_h * 4, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        blurred = blurred.scaled(
            max(2, int(covered.width())),
            max(2, int(covered.height())),
            Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation,
        )
        self._blur_cache = (key, blurred)
        return covered, blurred

    def _paint_frosted(self, painter, rect, opacity):
        radius = theme.PANEL_RADIUS
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        painter.save()
        painter.setOpacity(opacity)
        painter.setClipPath(path)

        backdrop = self._blurred_backdrop(rect)
        if backdrop is not None:
            target, image = backdrop
            painter.drawImage(target, image)

        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.PANEL_TINT)
        painter.drawRect(rect)
        painter.setClipping(False)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(theme.PANEL_EDGE, 1.0))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.restore()

    def _paint_flash(self, painter):
        zone, glyph_name, caption = self._flash
        progress = float(self._flash_anim.currentValue() or 0.0)
        fade = 1.0 - progress
        if fade <= 0.01:
            return

        centres = {"left": 0.22, "center": 0.5, "right": 0.78}
        cx = self.width() * centres.get(zone, 0.5)
        cy = self.height() / 2.0
        radius = 46 + 10 * progress

        painter.save()
        painter.setOpacity(fade)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 110))
        painter.drawEllipse(QRectF(cx - radius, cy - radius, radius * 2, radius * 2))

        painter.save()
        painter.translate(cx, cy - (8 if caption else 0))
        painter.scale(0.30, 0.30)
        painter.translate(-50, -50)
        painter.setBrush(theme.TEXT)
        painter.drawPath(icons.glyph(glyph_name))
        painter.restore()

        if caption:
            font = QFont(self.font())
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(theme.TEXT))
            painter.drawText(
                QRectF(cx - radius, cy + 8, radius * 2, 30),
                Qt.AlignHCenter | Qt.AlignTop,
                caption,
            )
        painter.restore()

    def flash(self, zone, glyph_name, caption=""):
        self._flash = (zone, glyph_name, caption)
        self._flash_anim.stop()
        self._flash_anim.start()

    def _clear_flash(self):
        self._flash = None
        self.update()

    # --- control visibility --------------------------------------------------
    def show_controls(self, restart_timer=True):
        if not self._player.has_media:
            return
        self._controls_shown = True
        if not self.controls.isVisible():
            self.controls.show()
        self._fade.stop()
        self._fade.setStartValue(self._overlay)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self.unsetCursor()
        if restart_timer:
            self._restart_hide_timer()

    def hide_controls(self):
        if not self.controls.isVisible():
            return
        self._controls_shown = False
        self._hide_timer.stop()
        self._fade.stop()
        self._fade.setStartValue(self._overlay)
        self._fade.setEndValue(0.0)
        self._fade.start()
        QTimer.singleShot(self._fade.duration() + 10, self._finish_hide)

    def _finish_hide(self):
        # A show() may have landed while the fade was still running.
        if not self._controls_shown:
            self.controls.hide()
            if self._player.is_playing:
                self.setCursor(Qt.BlankCursor)

    def toggle_controls(self):
        # Keyed off intent rather than the live opacity, so a second tap during
        # the fade reverses it instead of repeating the same action.
        if self._controls_shown:
            # Stop a stray mouse jitter from immediately undoing the hide.
            self._suppress_reveal_until = _now_ms() + 900
            self.hide_controls()
        else:
            self.show_controls()

    def _restart_hide_timer(self):
        self._hide_timer.stop()
        if self._player.is_playing and self._player.has_media:
            self._hide_timer.start(AUTO_HIDE_MS)

    # --- gestures ------------------------------------------------------------
    def _zone(self, x):
        ratio = x / max(1.0, float(self.width()))
        if ratio < 0.30:
            return "left"
        if ratio > 0.70:
            return "right"
        return "center"

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._press_pos is None:
            return super().mouseReleaseEvent(event)
        moved = (event.position().toPoint() - self._press_pos).manhattanLength()
        self._press_pos = None
        if moved > TAP_SLOP or not self._player.has_media:
            return super().mouseReleaseEvent(event)
        self._tap_timer.start(_tap_interval())
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton or not self._player.has_media:
            return super().mouseDoubleClickEvent(event)
        self._tap_timer.stop()
        zone = self._zone(event.position().x())
        if zone == "left":
            self._player.skip(-5000)
            self.flash("left", "rewind", "5s")
        elif zone == "right":
            self._player.skip(5000)
            self.flash("right", "forward", "5s")
        else:
            self._player.toggle()
            self.flash("center", "pause" if self._player.is_playing else "play")
        self.show_controls()
        event.accept()

    def _commit_single_tap(self):
        self.toggle_controls()

    def mouseMoveEvent(self, event):
        if self._player.has_media and _now_ms() >= self._suppress_reveal_until:
            self.show_controls()
        super().mouseMoveEvent(event)


def _tap_interval():
    """Double-tap window, kept snappy but never shorter than the system's."""
    system = QApplication.doubleClickInterval()
    return max(220, min(system, 320))


def _now_ms():
    return QDateTime.currentMSecsSinceEpoch()
