"""Application window: wires the player, the overlay UI and the exporters."""

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox

from . import theme
from .canvas import VideoCanvas
from .exporter import FFMPEG_HELP, ClipExporter, clip_target_path, find_ffmpeg, save_screenshot
from .player import PlayerController

VIDEO_FILTER = (
    "Video files (*.mp4 *.m4v *.mov *.mkv *.avi *.wmv *.webm *.flv *.mpg *.mpeg *.ts *.m2ts *.3gp);;"
    "All files (*.*)"
)

VIDEO_SUFFIXES = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm",
    ".flv", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp",
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Trim")
        self.setMinimumSize(*theme.MIN_WINDOW)
        self.resize(1280, 760)
        self.setAcceptDrops(True)

        self.player = PlayerController(self)
        self.canvas = VideoCanvas(self.player, self)
        self.setCentralWidget(self.canvas)

        self._ffmpeg = find_ffmpeg()
        self._export = None
        self._last_dir = QStandardPaths.writableLocation(QStandardPaths.MoviesLocation) or str(Path.home())

        self._connect()
        self.canvas.set_media_loaded(False)
        _use_dark_titlebar(self)

    # --- wiring --------------------------------------------------------------
    def _connect(self):
        controls = self.canvas.controls
        controls.openRequested.connect(self.open_dialog)
        controls.playPauseRequested.connect(self.player.toggle)
        controls.stopRequested.connect(self.player.stop)
        controls.skipRequested.connect(self.player.skip)
        controls.stepRequested.connect(self.player.step_frames)
        controls.repeatToggled.connect(self.player.set_repeat)
        controls.markerCycled.connect(self.cycle_marker)
        controls.saveClipRequested.connect(self.save_clip)
        controls.screenshotRequested.connect(self.save_screenshot)
        controls.muteToggled.connect(self.player.toggle_mute)
        controls.scrubber.seekRequested.connect(self.player.seek)

        self.canvas.openRequested.connect(self.open_dialog)

        self.player.positionChanged.connect(controls.set_position)
        self.player.durationChanged.connect(controls.set_duration)
        self.player.playingChanged.connect(controls.set_playing)
        self.player.markersChanged.connect(controls.set_markers)
        self.player.repeatChanged.connect(controls.set_repeat)
        self.player.mutedChanged.connect(controls.set_muted)
        self.player.errorRaised.connect(lambda msg: self.canvas.toast.show_message(msg, 4000))

    # --- opening -------------------------------------------------------------
    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select a video", self._last_dir, VIDEO_FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path):
        path = Path(path)
        if not path.is_file():
            self.canvas.toast.show_message("That file no longer exists.", 3200)
            return
        self._last_dir = str(path.parent)
        self.canvas.clear_frame()
        self.player.open(path)
        self.canvas.set_media_loaded(True)
        self.canvas.controls.set_markers(None, None)
        self.canvas.controls.set_position(0)
        self.setWindowTitle(f"{path.name} — Video Trim")
        self.canvas.toast.show_message(path.name, 1800)

    # --- actions -------------------------------------------------------------
    def cycle_marker(self):
        message = self.player.cycle_marker()
        if message:
            self.canvas.toast.show_message(message, 1800)

    def save_screenshot(self):
        if not self.player.has_media:
            return
        image = self.canvas.current_image()
        if image is None:
            self.canvas.toast.show_message("No frame to capture yet.", 2400)
            return
        try:
            target = save_screenshot(image, self.player.path, self.player.position)
        except Exception as exc:
            self.canvas.toast.show_message(f"Screenshot failed: {exc}", 4000)
            return
        self.canvas.toast.show_message(f"Saved  {target.name}", 2600)

    def save_clip(self):
        if not self.player.has_loop:
            self.canvas.toast.show_message("Set an A-B loop first.", 2400)
            return
        if self._export is not None and self._export.isRunning():
            self.canvas.toast.show_message("An export is already running.", 2400)
            return
        if not self._ffmpeg:
            QMessageBox.warning(self, "ffmpeg not found", FFMPEG_HELP)
            return

        a, b = self.player.markers
        target = clip_target_path(self.player.path, a, b)
        self.canvas.controls.btn_clip.setEnabled(False)
        self.canvas.toast.show_message("Exporting clip…  0%", 0)

        self._export = ClipExporter(self._ffmpeg, self.player.path, target, a, b, self)
        self._export.progress.connect(self._on_export_progress)
        self._export.finished_ok.connect(self._on_export_done)
        self._export.failed.connect(self._on_export_failed)
        self._export.start()

    def _on_export_progress(self, percent):
        self.canvas.toast.show_message(f"Exporting clip…  {percent}%", 0)

    def _on_export_done(self, path):
        self.canvas.controls.btn_clip.setEnabled(self.player.has_loop)
        self.canvas.toast.show_message(f"Saved  {Path(path).name}", 3200)

    def _on_export_failed(self, message):
        self.canvas.controls.btn_clip.setEnabled(self.player.has_loop)
        self.canvas.toast.show_message(message, 5000)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    # --- input ---------------------------------------------------------------
    def keyPressEvent(self, event):
        key = event.key()

        if key == Qt.Key_O:  # plain O and Ctrl+O both open
            self.open_dialog()
            return
        if not self.player.has_media:
            if key == Qt.Key_Escape and self.isFullScreen():
                self.showNormal()
                return
            return super().keyPressEvent(event)

        shift = event.modifiers() & Qt.ShiftModifier

        if key == Qt.Key_Space:
            self.player.toggle()
        elif key in (Qt.Key_Comma, Qt.Key_Less) or (key == Qt.Key_Left and shift):
            self.player.step_frames(-1)
        elif key in (Qt.Key_Period, Qt.Key_Greater) or (key == Qt.Key_Right and shift):
            self.player.step_frames(1)
        elif key == Qt.Key_Left:
            self.player.skip(-5000)
            self.canvas.flash("left", "rewind", "5s")
        elif key == Qt.Key_Right:
            self.player.skip(5000)
            self.canvas.flash("right", "forward", "5s")
        elif key == Qt.Key_Up:
            self.player.set_volume(self.player.volume + 0.05)
        elif key == Qt.Key_Down:
            self.player.set_volume(self.player.volume - 0.05)
        elif key == Qt.Key_Home:
            self.player.stop()
        elif key == Qt.Key_B:
            self.cycle_marker()
        elif key == Qt.Key_R:
            self.player.toggle_repeat()
        elif key == Qt.Key_M:
            self.player.toggle_mute()
        elif key == Qt.Key_S:
            self.save_screenshot()
        elif key == Qt.Key_C:
            self.save_clip()
        elif key in (Qt.Key_F, Qt.Key_F11):
            self.toggle_fullscreen()
        elif key == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
        else:
            return super().keyPressEvent(event)

        self.canvas.show_controls()

    def dragEnterEvent(self, event):
        if self._dropped_video(event) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = self._dropped_video(event)
        if path is not None:
            event.acceptProposedAction()
            self.open_path(path)

    @staticmethod
    def _dropped_video(event):
        data = event.mimeData()
        if not data.hasUrls():
            return None
        for url in data.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.suffix.lower() in VIDEO_SUFFIXES and path.is_file():
                return path
        return None

    def closeEvent(self, event):
        if self._export is not None and self._export.isRunning():
            self._export.cancel()
            self._export.wait(3000)
        self.player.pause()
        super().closeEvent(event)


def _use_dark_titlebar(window):
    """Match the Windows 11 title bar to the dark UI. No-op elsewhere."""
    if sys.platform != "win32":
        return
    try:
        window.winId()  # force native handle creation
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            int(window.winId()),
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        pass


def _apply_palette(app):
    palette = QPalette()
    palette.setColor(QPalette.Window, theme.BG)
    palette.setColor(QPalette.Base, theme.BG)
    palette.setColor(QPalette.WindowText, theme.TEXT)
    palette.setColor(QPalette.Text, theme.TEXT)
    palette.setColor(QPalette.Button, QColor(28, 31, 39))
    palette.setColor(QPalette.ButtonText, theme.TEXT)
    palette.setColor(QPalette.Highlight, theme.ACCENT)
    palette.setColor(QPalette.HighlightedText, QColor(10, 12, 16))
    palette.setColor(QPalette.ToolTipBase, QColor(24, 27, 34))
    palette.setColor(QPalette.ToolTipText, theme.TEXT)
    app.setPalette(palette)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName("Video Trim")
    app.setStyle("Fusion")
    _apply_palette(app)

    window = MainWindow()
    window.show()

    # `python app.py <file>` opens straight into a video.
    for candidate in argv[1:]:
        if Path(candidate).is_file():
            window.open_path(candidate)
            break

    return app.exec()
