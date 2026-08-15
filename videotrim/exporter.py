"""Saving media to disk: PNG stills and ffmpeg-cut A-B clips."""

import os
import subprocess

from PySide6.QtCore import QThread, Signal

from .ffmpeg_tools import NO_WINDOW as _NO_WINDOW
from .ffmpeg_tools import clip_command
from .naming import clip_name, frame_name
from .paths import output_dir, unique_path


def save_screenshot(image, source_path, position_ms):
    """Write the given QImage to the Desktop as a PNG. Returns the path."""
    target = unique_path(output_dir() / frame_name(source_path, position_ms))
    if not image.save(str(target), "PNG"):
        raise OSError(f"Could not write {target}")
    return target


def clip_target_path(source_path, a_ms, b_ms):
    return unique_path(output_dir() / clip_name(source_path, a_ms, b_ms))


class ClipExporter(QThread):
    """Runs one ffmpeg trim on a worker thread, reporting progress as a percent."""

    progress = Signal(int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, ffmpeg, source, target, a_ms, b_ms, parent=None):
        super().__init__(parent)
        self._ffmpeg = ffmpeg
        self._source = str(source)
        self._target = str(target)
        self._a = int(a_ms)
        self._duration = max(1, int(b_ms) - int(a_ms))
        self._cancelled = False
        self._process = None

    def cancel(self):
        self._cancelled = True
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
            except Exception:
                pass

    def _command(self):
        return clip_command(
            self._ffmpeg, self._source, self._target, self._a, self._a + self._duration
        )

    def run(self):
        try:
            self._process = subprocess.Popen(
                self._command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                universal_newlines=True,
                creationflags=_NO_WINDOW,
            )
        except Exception as exc:  # pragma: no cover - depends on local install
            self.failed.emit(f"Could not start ffmpeg: {exc}")
            return

        for line in self._process.stdout:
            if self._cancelled:
                break
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    done = int(line.split("=", 1)[1]) / 1000.0
                except ValueError:
                    continue
                self.progress.emit(int(max(0.0, min(100.0, done / self._duration * 100.0))))

        stderr = ""
        try:
            stderr = self._process.stderr.read() or ""
        except Exception:
            pass
        code = self._process.wait()

        if self._cancelled:
            self._cleanup_partial()
            self.failed.emit("Export cancelled.")
        elif code == 0 and os.path.exists(self._target):
            self.progress.emit(100)
            self.finished_ok.emit(self._target)
        else:
            self._cleanup_partial()
            detail = stderr.strip().splitlines()[-1] if stderr.strip() else f"exit code {code}"
            self.failed.emit(f"ffmpeg failed: {detail}")

    def _cleanup_partial(self):
        try:
            if os.path.exists(self._target):
                os.remove(self._target)
        except OSError:
            pass
