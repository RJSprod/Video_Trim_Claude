"""Background ffmpeg jobs, polled by the browser for a live percentage.

The desktop app reports export progress through Qt signals into a toast. Over
HTTP there is nobody to push to, so a job gets an id, runs on its own thread and
the page polls it — same toast, same percentage, one round trip behind.
"""

import secrets
import threading
import time
from pathlib import Path

from .. import ffmpeg_tools
from ..ffmpeg_tools import FFmpegError
from ..security.fs_boundary import (
    FilesystemPolicyError,
    is_internal,
    safe_internal_unlink,
)

# A finished job stays queryable this long, so a poll that lands just after the
# encode ends still sees the result rather than a 404.
_RETAIN_SECONDS = 30 * 60


class Job:
    """One ffmpeg run: pending → running → done | failed | cancelled."""

    def __init__(self, job_id, kind, label, total_ms, command, target, on_success=None,
                 owner_session="", owner_ip=""):
        self.id = job_id
        self.kind = kind
        self.label = label
        self.total_ms = max(1, int(total_ms))
        self.command = command
        # Every job renders into the app's own cache. A job target is never
        # external, which is what makes the cancellation cleanup below safe: it
        # can only ever remove something this app staged.
        if not is_internal(target):
            raise FilesystemPolicyError(
                "A render job may only target a path inside the installation."
            )
        self.target = Path(target)
        # Recorded so a permission revoked mid-render can be matched to the work
        # it should stop.
        self.owner_session = str(owner_session or "")
        self.owner_ip = str(owner_ip or "")
        self.state = "pending"
        self.percent = 0
        self.error = ""
        self.result = {}
        self.started = time.time()
        self.finished = None
        self._cancel = threading.Event()
        self._on_success = on_success
        self._lock = threading.Lock()

    # --- reporting -----------------------------------------------------------
    def snapshot(self):
        with self._lock:
            data = {
                "id": self.id,
                "kind": self.kind,
                "label": self.label,
                "state": self.state,
                "percent": self.percent,
            }
            if self.error:
                data["error"] = self.error
            data.update(self.result)
            return data

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    @property
    def done(self):
        return self.state in ("done", "failed", "cancelled")

    # --- running -------------------------------------------------------------
    def _progress(self, percent):
        with self._lock:
            # Never let a late line walk the number backwards.
            self.percent = max(self.percent, int(percent))

    def run(self):
        with self._lock:
            self.state = "running"
        try:
            ffmpeg_tools.run_with_progress(
                self.command,
                self.total_ms,
                on_progress=self._progress,
                cancelled=lambda: self._cancel.is_set(),
            )
        except FFmpegError as exc:
            self._fail("Export cancelled." if self.cancelled else str(exc))
            return
        except Exception as exc:  # pragma: no cover - unexpected local failure
            self._fail(f"Export failed: {exc}")
            return

        if self.cancelled:
            self._fail("Export cancelled.")
            return
        if not self.target.is_file():
            self._fail("ffmpeg reported success but wrote no file.")
            return

        extra = {}
        if self._on_success is not None:
            try:
                extra = self._on_success(self) or {}
            except Exception as exc:  # pragma: no cover
                self._fail(f"Export finished but could not be published: {exc}")
                return

        with self._lock:
            self.percent = 100
            self.state = "done"
            self.result = extra
            self.finished = time.time()

    def _fail(self, message):
        self._discard_partial()
        with self._lock:
            self.state = "cancelled" if self.cancelled else "failed"
            self.error = message
            self.finished = time.time()

    def _discard_partial(self):
        """Remove the half-written render. Internal paths only.

        This used to unlink ``self.target`` unconditionally, and for clip jobs
        that target was a file on the user's Desktop — so a failed encode
        deleted whatever happened to be sitting at that name. Targets are now
        internal by construction, and this proves it again before removing
        anything.
        """
        try:
            safe_internal_unlink(self.target)
        except FilesystemPolicyError:
            pass


class JobRegistry:
    """Starts jobs and keeps their results around long enough to be collected."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}

    def start(self, kind, label, total_ms, command, target, on_success=None,
              owner_session="", owner_ip=""):
        job = Job(secrets.token_urlsafe(12), kind, label, total_ms, command, target,
                  on_success, owner_session=owner_session, owner_ip=owner_ip)
        with self._lock:
            self._prune()
            self._jobs[job.id] = job
        threading.Thread(target=job.run, name=f"vt-{kind}-{job.id}", daemon=True).start()
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(str(job_id))

    def active(self, kind=None):
        with self._lock:
            return [
                job for job in self._jobs.values()
                if not job.done and (kind is None or job.kind == kind)
            ]

    def cancel_all(self):
        for job in self.active():
            job.cancel()

    def cancel_for_session(self, session_id):
        """Stop work owned by a session that just ended or lost permission."""
        stopped = 0
        for job in self.active():
            if job.owner_session and job.owner_session == session_id:
                job.cancel()
                stopped += 1
        return stopped

    def cancel_for_ip(self, ip):
        """Stop work owned by an address the host just revoked.

        Only reaches jobs that are still entirely internal — anything already
        past the external commit is finished and stays finished, because the
        filesystem invariant outranks a late revocation.
        """
        stopped = 0
        for job in self.active():
            if job.owner_ip and job.owner_ip == ip:
                job.cancel()
                stopped += 1
        return stopped

    def _prune(self):
        cutoff = time.time() - _RETAIN_SECONDS
        for job_id, job in list(self._jobs.items()):
            if job.done and (job.finished or 0) < cutoff:
                self._jobs.pop(job_id, None)
