"""Saving media to disk from the desktop app: PNG stills and ffmpeg-cut clips.

The desktop app runs as the host user with no login and no IP policy, which
makes it inherently host-admin — but not exempt. It renders into the same
internal staging area as the WebUI and publishes through the same exclusive-
create gateway, so the host-protection promise does not depend on which front
end someone happened to open.

Behaviour change for existing desktop users: saves no longer default to the
Desktop folder. The app reads the ``save_location`` setting the host chose and
refuses to export until one exists, with no fallback.
"""

import os
import subprocess

from PySide6.QtCore import QThread, Signal

from .config.settings import SettingsService
from .config.store import CommitJournal, Store
from .ffmpeg_tools import NO_WINDOW as _NO_WINDOW
from .ffmpeg_tools import clip_command, verify_executable
from .naming import clip_name, frame_name
from .security import fs_boundary
from .security.fs_boundary import CollisionPolicy, OutputRootError

# Desktop renders stage here, exactly like the WebUI's.
STAGING_DIR = fs_boundary.CACHE_DIR / "exports"

NO_SAVE_LOCATION = (
    "Video Trim has no save location yet.\n\n"
    "Choose one in the WebUI's Settings (or start the WebUI once with "
    "--output-dir) and it will be used here too. Saves are never written to a "
    "folder you did not pick."
)


def _services():
    store = Store()
    return store, SettingsService(store), CommitJournal(store)


def _staging_dir(job_id):
    """A fresh per-job folder, so a staging name can never collide."""
    return fs_boundary.ensure_internal_dir(STAGING_DIR / str(job_id))


def _publish(staged, desired_name, job_id, policy=CollisionPolicy.UNIQUE_NEW_NAME):
    """Hand a finished internal file to the gateway. Returns the published path."""
    _store, settings, journal = _services()
    try:
        root = settings.save_location()
    except OutputRootError as exc:
        raise OSError(f"{NO_SAVE_LOCATION}\n\n({exc})") from exc

    result = fs_boundary.commit_new_file(
        staged,
        desired_name,
        root,
        collision_policy=policy,
        journal=journal,
        job_id=str(job_id),
    )
    if result.status == "possible_partial":
        raise OSError(
            f"Saving failed and a new file named {result.basename} may be "
            "incomplete in your save folder. Video Trim will not delete or "
            "repair it — check it yourself."
        )
    fs_boundary.safe_internal_unlink(staged)
    return result.path


def save_screenshot(image, source_path, position_ms):
    """Render the still internally, then publish it. Returns the saved path.

    The QImage never touches an external path: it is written into the app's own
    cache and copied out through the gateway, so a still can no longer overwrite
    something that happens to share its generated name.
    """
    job_id = f"still-{os.getpid()}-{int(position_ms)}"
    staging = _staging_dir(job_id)
    desired = frame_name(source_path, position_ms)
    staged = staging / "frame.png"
    if not image.save(str(staged), "PNG"):
        raise OSError("Could not render the still.")
    try:
        return _publish(staged, desired, job_id)
    finally:
        fs_boundary.safe_internal_rmtree(staging)


def clip_staging_path(source_path, a_ms, b_ms, job_id):
    """Where the desktop clip renders before it is published."""
    return _staging_dir(job_id) / "clip.mp4"


def clip_desired_name(source_path, a_ms, b_ms):
    return clip_name(source_path, a_ms, b_ms)


def publish_clip(staged, source_path, a_ms, b_ms, job_id):
    """Copy a finished desktop clip into the host's save folder."""
    try:
        return _publish(staged, clip_desired_name(source_path, a_ms, b_ms), job_id)
    finally:
        fs_boundary.safe_internal_rmtree(os.path.dirname(str(staged)))


class ClipExporter(QThread):
    """Runs one ffmpeg trim on a worker thread, reporting progress as a percent.

    ``target`` is an internal staging path. Publication happens afterwards,
    through the gateway, on the ``finished_ok`` path.
    """

    progress = Signal(int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, ffmpeg, source, target, a_ms, b_ms, parent=None):
        super().__init__(parent)
        self._ffmpeg = ffmpeg
        self._source = str(source)
        self._target = str(fs_boundary.require_internal(target, what="render target"))
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
            command = self._command()
            command[0] = verify_executable(command[0])
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                universal_newlines=True,
                creationflags=_NO_WINDOW,
                shell=False,
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
        """Only ever removes the internal staging file this job was writing."""
        fs_boundary.safe_internal_unlink(self._target)
