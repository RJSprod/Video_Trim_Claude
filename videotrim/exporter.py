"""Saving media to disk: PNG stills and ffmpeg-cut A-B clips."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from . import theme
from .paths import desktop_dir, sanitize, unique_path

# Keep the console window from flashing up on Windows for every ffmpeg call.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def find_ffmpeg():
    """Locate an ffmpeg binary: alongside the app, on PATH, or pip-installed."""
    here = Path(__file__).resolve().parent.parent
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for candidate in (here / exe, here / "ffmpeg" / exe, here / "ffmpeg" / "bin" / exe):
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("ffmpeg")
    if found:
        return found

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG_HELP = (
    "ffmpeg is needed to cut clips but was not found.\n\n"
    "Install it with either:\n"
    "    pip install imageio-ffmpeg\n"
    "    winget install Gyan.FFmpeg\n\n"
    "…then restart the app."
)


def save_screenshot(image, source_path, position_ms):
    """Write the given QImage to the Desktop as a PNG. Returns the path."""
    stem = sanitize(Path(source_path).stem) if source_path else "frame"
    name = f"{stem}_frame_{theme.fmt_time_filename(position_ms)}.png"
    target = unique_path(desktop_dir() / name)
    if not image.save(str(target), "PNG"):
        raise OSError(f"Could not write {target}")
    return target


def clip_target_path(source_path, a_ms, b_ms):
    stem = sanitize(Path(source_path).stem)
    name = (
        f"{stem}_clip_{theme.fmt_time_filename(a_ms)}"
        f"_to_{theme.fmt_time_filename(b_ms)}.mp4"
    )
    return unique_path(desktop_dir() / name)


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
        return [
            self._ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel", "error",
            "-y",
            # Input seeking plus re-encode: ffmpeg decodes from the preceding
            # keyframe and discards, so the cut lands exactly on the A marker.
            "-ss", f"{self._a / 1000.0:.3f}",
            "-i", self._source,
            "-t", f"{self._duration / 1000.0:.3f}",
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            self._target,
        ]

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
