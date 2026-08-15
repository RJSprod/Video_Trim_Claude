"""The one route every saved file takes out of this application.

Features render into ``cache/…``, then call :meth:`OutputService.publish`. There
is no second way out: no feature module imports the gateway directly, none of
them knows the save location, and none of them can name a destination path.
"""

import secrets

from ..security import fs_boundary
from ..security.fs_boundary import (
    CollisionPolicy,
    CommitDenied,
    FilesystemPolicyError,
    OutputRootError,
)

# Distinct on purpose. "Already exists" is about a file the app did not create;
# the partial message is about one it may have. Reporting the first when the
# second is true is how a user ends up trusting a corrupt file.
ALREADY_EXISTS = "Already exists — skipped"
POSSIBLE_PARTIAL = "A previous transfer may have left an incomplete file with this name"
NO_SAVE_LOCATION = "Save location is unavailable"


class PublishOutcome:
    """What the caller tells the user, and what the UI styles it as."""

    def __init__(self, status, message, basename="", path=None, download=None):
        self.status = status          # saved | already_exists | possible_partial
        self.message = message
        self.basename = basename
        self.path = path
        self.download = download or {}

    def as_dict(self):
        payload = {
            "status": self.status,
            "message": self.message,
            "saved_name": self.basename,
        }
        payload.update(self.download)
        return payload


class OutputService:
    """Staging directories, and the single external commit."""

    def __init__(self, settings, journal, registry=None):
        self._settings = settings
        self._journal = journal
        self._registry = registry

    # --- staging -------------------------------------------------------------
    @staticmethod
    def new_job_id():
        return secrets.token_urlsafe(9)

    def staging_dir(self, kind, job_id):
        """A fresh per-job folder under ``cache/``.

        Fresh per job so a staging name can never collide with another job's —
        which is also why ffmpeg's ``-n`` never fires in normal operation.
        """
        return fs_boundary.ensure_internal_dir(
            fs_boundary.CACHE_DIR / kind / str(job_id)
        )

    def discard_staging(self, kind, job_id):
        fs_boundary.safe_internal_rmtree(fs_boundary.CACHE_DIR / kind / str(job_id))

    # --- destination reporting ----------------------------------------------
    def destination_for(self, host_local):
        """Path for the host, label for everyone else. Never both."""
        return self._settings.destination_for(host_local)

    def is_configured(self):
        return self._settings.is_configured()

    # --- the commit ----------------------------------------------------------
    def publish(self, staged_path, desired_basename, policy, authorize=None,
                job_id="", session=None, host_local=False, kind="saved_output"):
        """Copy a finished internal file into the host's save folder.

        Everything that could refuse has already refused by the time bytes move:
        the destination is re-resolved and re-vetted, the name is rejected rather
        than rewritten if it is unsafe, permission is re-checked through
        ``authorize`` and an exclusive create decides whether the name is free.
        """
        try:
            root = self._settings.save_location()
        except OutputRootError as exc:
            raise OutputUnavailable(self._explain(exc, host_local)) from exc

        try:
            result = fs_boundary.commit_new_file(
                staged_path,
                desired_basename,
                root,
                collision_policy=policy,
                journal=self._journal,
                job_id=job_id,
                authorize=authorize,
            )
        except CommitDenied:
            raise
        except FilesystemPolicyError as exc:
            raise OutputUnavailable(self._explain(exc, host_local)) from exc

        if result.status == "already_exists":
            # A name the journal knows about might be one of ours that never
            # finished, so say the more careful of the two things.
            if self._journal.knows_name(str(root), result.basename):
                return PublishOutcome("possible_partial", POSSIBLE_PARTIAL,
                                      basename=result.basename)
            return PublishOutcome("already_exists", ALREADY_EXISTS,
                                  basename=result.basename)

        if result.status == "possible_partial":
            return PublishOutcome(
                "possible_partial",
                f"Saving failed, and a new file named {result.basename} may be "
                "incomplete. Video Trim will not delete or repair it.",
                basename=result.basename,
            )

        download = {}
        if self._registry is not None and session is not None:
            token = self._registry.add(
                result.path, kind="saved", session_id=session.id,
                capability="saved_output",
            )
            download = {"download_url": f"/vt/saved/{token}"}

        return PublishOutcome(
            "saved",
            f"Saved  {result.basename}",
            basename=result.basename,
            path=result.path,
            download=download,
        )

    @staticmethod
    def _explain(exc, host_local):
        """Error text that never leaks a host path to a remote session."""
        if host_local:
            return str(exc)
        return NO_SAVE_LOCATION


class OutputUnavailable(Exception):
    """The save location is missing, unusable, or overlaps the installation."""


__all__ = [
    "ALREADY_EXISTS",
    "CollisionPolicy",
    "NO_SAVE_LOCATION",
    "OutputService",
    "OutputUnavailable",
    "POSSIBLE_PARTIAL",
    "PublishOutcome",
]
