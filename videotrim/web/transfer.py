"""Media Transfer: a phone sends a photo or a video to the host's save folder.

The whole feature is one direction and one verb. It creates new files, and that
is all it can do — there is no delete, no overwrite, no rename and no subfolder
creation anywhere in this module, and it cannot name a destination path because
it never sees one.

Duplicates are decided by final filename, as asked for. A name that already
exists is skipped, never turned into "name (2).ext" — that behaviour belongs to
generated Video Trim output, not to a file the user already has.
"""

import shutil
from pathlib import Path

from fastapi import HTTPException, Request

from ..security.fs_boundary import (
    CommitDenied,
    UnsafeNameError,
    sanitize_basename,
)
from .media import IMAGE_SUFFIXES, TRANSFERABLE_SUFFIXES, VIDEO_SUFFIXES
from .output import CollisionPolicy, OutputUnavailable

_CHUNK = 1024 * 1024

# Leave this much free rather than filling the staging disk. Note the staging
# volume and the save-location volume can differ, so both are checked.
STAGING_HEADROOM = 2 * 1024 ** 3

# Generous, but not unbounded: an authenticated write-allowed client should not
# be able to fill the host's disk with one request by accident.
MAX_TRANSFER_BYTES = 32 * 1024 ** 3


def register_transfer_routes(app, state):
    @app.get("/vt/api/transfer/status")
    def transfer_status(request: Request):
        """What this device may do right now, and where things land."""
        session = state.session_for(request)
        decision = state.write_policy.evaluate(request, session)
        host_local = state.host_guard.is_host_request(request)
        return {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "can_request_access": decision.can_request,
            "access_requested": decision.requested,
            "destination": state.output.destination_for(host_local),
            "output_configured": state.output.is_configured(),
            "accepts": sorted(TRANSFERABLE_SUFFIXES),
            "max_bytes": MAX_TRANSFER_BYTES,
        }

    @app.post("/vt/api/transfer/request-access")
    def request_access(request: Request):
        """Ask the host to allow this address. Grants nothing on its own.

        Sets a timestamp the host sees on their own machine, and nothing else.
        There is no approval token, no remote approval, and no path by which
        waiting long enough turns into permission.
        """
        session = state.session_for(request)
        decision = state.write_policy.evaluate(request, session)
        if decision.allowed:
            return {"status": "already_allowed"}
        state.write_policy.request_access(request, session, state.access_throttle)
        return {
            "status": "requested",
            "message": "Access requested — the host will see this on their machine",
        }

    @app.post("/vt/api/transfer/upload")
    async def transfer_upload(request: Request, name: str = ""):
        """Stream one file to internal staging, then publish it.

        Streamed rather than buffered because the point of the feature is a
        multi-gigabyte video from a phone, and reading that into RAM first would
        make the host's memory the limiting factor.
        """
        session = state.session_for(request)
        host_local = state.host_guard.is_host_request(request)

        decision = state.write_policy.evaluate(request, session)
        if not decision.allowed:
            raise HTTPException(status_code=403, detail=decision.reason)

        try:
            # The raw name, not Path(name).name. Taking the basename would turn
            # "../holiday.jpg" and "holiday.jpg" into one request, and two
            # distinct names collapsing onto one file is precisely what the
            # rejection list exists to prevent. Nothing escapes either way, but
            # a caller that sent a path gets told so rather than getting a
            # different file than it asked for.
            safe_name = sanitize_basename(str(name or ""))
        except UnsafeNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        suffix = Path(safe_name).suffix.lower()
        if suffix not in TRANSFERABLE_SUFFIXES:
            raise HTTPException(
                status_code=415,
                detail=f"{suffix or 'That file'} is not a supported photo or video.",
            )

        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_TRANSFER_BYTES:
            raise HTTPException(status_code=413, detail="That file is too large to send.")

        job_id = state.output.new_job_id()
        staging = state.output.staging_dir("transfers", job_id)
        staged = staging / safe_name

        if declared and declared.isdigit():
            free = shutil.disk_usage(str(staging)).free
            if int(declared) + STAGING_HEADROOM > free:
                state.output.discard_staging("transfers", job_id)
                raise HTTPException(
                    status_code=507,
                    detail="There is not enough free space on the host for that file.",
                )

        written = 0
        try:
            with open(staged, "wb") as handle:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > MAX_TRANSFER_BYTES:
                        raise HTTPException(status_code=413,
                                            detail="That file is too large to send.")
                    handle.write(chunk)
            if written == 0:
                raise HTTPException(status_code=400, detail="That file was empty.")
        except HTTPException:
            state.output.discard_staging("transfers", job_id)
            raise
        except Exception as exc:
            state.output.discard_staging("transfers", job_id)
            raise HTTPException(status_code=500, detail=f"Transfer failed: {exc}") from exc

        if suffix in VIDEO_SUFFIXES:
            _validate_video(state, staged, job_id)
        elif suffix in IMAGE_SUFFIXES:
            _validate_image(state, staged, job_id)

        try:
            outcome = state.output.publish(
                staged,
                safe_name,
                CollisionPolicy.SKIP_BY_NAME,
                authorize=state.write_policy.authorizer(request, session),
                job_id=job_id,
                session=session,
                host_local=host_local,
                kind="transfer",
            )
        except CommitDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except OutputUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            state.output.discard_staging("transfers", job_id)

        return outcome.as_dict()


def _validate_video(state, staged, job_id):
    """Confirm a video is actually decodable before it is copied out.

    Probing runs a media demuxer over bytes a remote client chose, so it is
    exactly as untrusted as the upload itself — which is why the probe carries
    the same protocol whitelist as a render.
    """
    from .. import ffmpeg_tools

    if not state.ffmpeg and not state.ffprobe:
        return  # nothing to validate with; the extension check already ran
    try:
        ffmpeg_tools.probe_video(staged, ffmpeg=state.ffmpeg, ffprobe=state.ffprobe)
    except ffmpeg_tools.FFmpegError as exc:
        state.output.discard_staging("transfers", job_id)
        raise HTTPException(
            status_code=415, detail=f"That video could not be read: {exc}"
        ) from exc


_IMAGE_MAGIC = (
    b"\xff\xd8\xff",              # JPEG
    b"\x89PNG\r\n\x1a\n",         # PNG
    b"GIF87a", b"GIF89a",         # GIF
    b"BM",                        # BMP
    b"II*\x00", b"MM\x00*",       # TIFF (also DNG)
)


def _validate_image(state, staged, job_id):
    """A lightweight signature check. Cheap, and it never invokes a decoder."""
    try:
        with open(staged, "rb") as handle:
            head = handle.read(32)
    except OSError:
        return
    if any(head.startswith(magic) for magic in _IMAGE_MAGIC):
        return
    # RIFF/ISO-BMFF containers carry their brand a few bytes in.
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return
    if head[4:8] == b"ftyp":  # HEIC/HEIF/AVIF
        return
    state.output.discard_staging("transfers", job_id)
    raise HTTPException(
        status_code=415,
        detail="That file does not look like a photo Video Trim can accept.",
    )
