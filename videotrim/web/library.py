"""Files: a read-only window onto the folder the host chose to save into.

The rest of the application is careful never to tell a browser where anything
lives. This feature is the one place a client names a file at all, so the rules
it works under are narrow and stated here rather than spread across routes:

    ROOT        exactly the configured save location, resolved fresh per request
    NAMES       relative to that root, always; an absolute path is never sent to
                a session that is not the host's own, and never accepted from any
    READS       listing a folder, serving a picture or a video, and rendering a
                poster frame into the app's own cache
    WRITES      none. Not a rename, not a delete, not a new folder. There is no
                code path in this module that can change a file the user owns,
                and the gateway it would have to go through is not imported.

Everything a client asks for goes through ``fs_boundary.resolve_within_root``,
which refuses ``..``, drive letters and symlinks that leave the folder — so the
worst a crafted path can do is name something that is already inside the folder
the host deliberately pointed this application at.
"""

import hashlib
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Request

from .. import ffmpeg_tools
from ..ffmpeg_tools import FFmpegError
from ..security import fs_boundary
from ..security.fs_boundary import FilesystemPolicyError, OutputRootError
from .media import IMAGE_SUFFIXES, VIDEO_SUFFIXES, file_response
from .parsing import read_json

# What the browser can actually show. Everything else is listed — the folder is
# the user's own and hiding files from them would be a lie — but only these are
# served, because "you can see it in the list" and "the app will hand you the
# bytes" are different promises.
VIEWABLE_SUFFIXES = VIDEO_SUFFIXES | IMAGE_SUFFIXES

AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".aiff"}

# Roughly a screenful of icons at the largest size, so one folder cannot make
# one response enormous. The UI says when a folder was longer than this.
MAX_ENTRIES = 2000

# Poster frames are decoded one or two at a time. A folder of two hundred
# videos otherwise starts two hundred ffmpeg processes the moment it scrolls
# into view, and the host notices.
_POSTER_GATE = threading.Semaphore(2)
_POSTER_SECONDS = 3
_POSTER_MAX_AGE = 30 * 24 * 3600


def kind_of(path, is_dir=False):
    """One of folder / video / image / audio / file."""
    if is_dir:
        return "folder"
    suffix = path.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return "file"


def type_label(name, kind):
    """The words in the Type column: "MP4 video", "JPG image", "Folder"."""
    if kind == "folder":
        return "Folder"
    suffix = Path(name).suffix.lstrip(".").upper()
    if not suffix:
        return "File"
    noun = {"video": "video", "image": "image", "audio": "audio"}.get(kind, "file")
    return f"{suffix} {noun}"


def _thumb_dir():
    """Resolved per call, so a relocated cache root is picked up rather than
    remembered from import time."""
    return fs_boundary.CACHE_DIR / "thumbs"


def register_library_routes(app, state):
    """Wire the Files tool's routes onto the application."""

    # --- shared helpers ------------------------------------------------------
    def root_for(request):
        """The save location as it is *right now*, or a 503 that says why.

        Re-resolved on every request rather than cached: a drive can be
        unplugged between two clicks, and the honest answer then is "that folder
        is not there", not a stale listing.
        """
        try:
            return state.settings.save_location()
        except OutputRootError as exc:
            detail = (
                str(exc) if state.is_host_admin(request)
                else "The host has not chosen a save folder yet, so there is nothing to browse."
            )
            raise HTTPException(status_code=503, detail=detail) from exc

    def resolve(request, root, relative):
        try:
            return fs_boundary.resolve_within_root(root, relative)
        except FilesystemPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def relative_to_root(root, target):
        """The name a client sees: relative, posix-style, "" for the root itself."""
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - resolve_within_root prevents this
            raise HTTPException(status_code=403, detail="That path is outside the save folder.")
        return "" if relative == "." else relative

    def readable_file(request, path_param):
        """Resolve a client's path to a regular file it is allowed to be given."""
        root = root_for(request)
        target = resolve(request, root, path_param)
        if target.is_dir():
            raise HTTPException(status_code=400, detail="That is a folder, not a file.")
        try:
            target = fs_boundary.validate_external_read(target)
        except FilesystemPolicyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if target.suffix.lower() not in VIEWABLE_SUFFIXES:
            raise HTTPException(
                status_code=415,
                detail="Video Trim only opens pictures and videos from this folder.",
            )
        return root, target

    # --- listing -------------------------------------------------------------
    @app.get("/vt/api/library/list")
    def library_list(request: Request, path: str = ""):
        """One folder, as names and metadata. No side effects of any kind."""
        session = state.require_session(request)
        root = root_for(request)
        current = resolve(request, root, path)

        if not current.is_dir():
            raise HTTPException(status_code=404, detail="That folder is not there any more.")

        entries = []
        truncated = False
        try:
            for item in sorted(current.iterdir(), key=lambda p: p.name.lower()):
                if item.name.startswith("."):
                    continue
                if len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
                try:
                    info = item.stat()
                    is_dir = item.is_dir()
                except OSError:
                    continue
                kind = kind_of(item, is_dir)
                entries.append({
                    "name": item.name,
                    "path": relative_to_root(root, current / item.name),
                    "kind": kind,
                    "type": type_label(item.name, kind),
                    "ext": item.suffix.lstrip(".").lower(),
                    "size": 0 if is_dir else int(info.st_size),
                    "modified": float(info.st_mtime),
                    "viewable": (not is_dir) and item.suffix.lower() in VIEWABLE_SUFFIXES,
                })
        except PermissionError as exc:
            raise HTTPException(status_code=403,
                                detail="That folder is not readable.") from exc
        except OSError as exc:
            raise HTTPException(status_code=404,
                                detail="That folder could not be read.") from exc

        here = relative_to_root(root, current)
        crumbs = [{"name": "Save folder", "path": ""}]
        walked = []
        for part in [p for p in here.split("/") if p]:
            walked.append(part)
            crumbs.append({"name": part, "path": "/".join(walked)})

        counts = {}
        for entry in entries:
            counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1

        host_local = state.is_host_admin(request)
        return {
            # Relative, always. The absolute path appears only in root_label,
            # and only when the server decided this session may see one.
            "path": here,
            "parent": "/".join(here.split("/")[:-1]) if here else None,
            "crumbs": crumbs,
            "root_label": state.output.destination_for(host_local),
            "entries": entries,
            "counts": counts,
            "truncated": truncated,
            "ffmpeg": bool(state.ffmpeg),
            "can_play": bool(state.ffmpeg),
            "session": session.username,
        }

    # --- bytes ---------------------------------------------------------------
    @app.api_route("/vt/library/file", methods=["GET", "HEAD"])
    def library_file(request: Request, path: str = "", download: int = 0):
        """Serve a picture or a video from the save folder, Range and all."""
        state.require_session(request)
        _root, target = readable_file(request, path)
        return file_response(
            target,
            range_header=request.headers.get("range"),
            head_only=request.method == "HEAD",
            download_name=target.name if download else None,
        )

    @app.get("/vt/library/poster")
    def library_poster(request: Request, path: str = "", width: int = 480):
        """A cached poster frame for a video, so icon views have pictures.

        Rendered into ``cache/thumbs`` — inside the installation, like every
        other thing this app writes for itself — and keyed by the source's size
        and modification time, so a replaced file gets a new poster rather than
        the old one.
        """
        state.require_session(request)
        _root, target = readable_file(request, path)
        if target.suffix.lower() not in VIDEO_SUFFIXES:
            raise HTTPException(status_code=415, detail="That file has no poster frame.")
        ffmpeg = state.require_ffmpeg()

        width = max(120, min(720, int(width or 480)))
        info = target.stat()
        key = hashlib.sha256(
            f"{target}|{info.st_size}|{int(info.st_mtime)}|{width}".encode("utf-8", "replace")
        ).hexdigest()[:24]
        poster = fs_boundary.ensure_internal_dir(_thumb_dir()) / f"{key}.jpg"

        if not poster.is_file() or poster.stat().st_size == 0:
            with _POSTER_GATE:
                # Another request may have rendered it while this one waited.
                if not poster.is_file() or poster.stat().st_size == 0:
                    if poster.exists():
                        fs_boundary.safe_internal_unlink(poster)
                    try:
                        ffmpeg_tools.extract_poster(
                            ffmpeg, target, poster, _POSTER_SECONDS * 1000, width
                        )
                    except (FFmpegError, OSError) as exc:
                        raise HTTPException(
                            status_code=422,
                            detail="No frame could be read from that file.",
                        ) from exc

        return file_response(poster, range_header=None, head_only=False)

    # --- opening in the player ----------------------------------------------
    @app.post("/vt/api/library/open")
    async def library_open(request: Request):
        """Hand the player a video from the save folder.

        Same describe() the upload and host-browse paths use, so the player
        cannot tell where a file came from — and the same per-session token, so
        the URL it gets back is worthless to anybody else.
        """
        session = state.require_session(request)
        body = await read_json(request)
        _root, target = readable_file(request, str(body.get("path") or ""))
        if target.suffix.lower() not in VIDEO_SUFFIXES:
            raise HTTPException(status_code=415, detail="That file is not a video.")

        try:
            return state.describe(target, session, request, capability="library")
        except FFmpegError as exc:
            # A file ffmpeg cannot read may still be one the browser plays, so
            # this is a thinner answer rather than a refusal.
            if not state.ffmpeg:
                token = state.registry.add(target, kind="source", session_id=session.id,
                                           capability="library")
                return {
                    "token": token,
                    "name": target.name,
                    "duration_ms": 0,
                    "fps": 0,
                    "width": 0,
                    "height": 0,
                    "codec": "",
                    "media_url": f"/vt/media/{token}",
                    "likely_playable": True,
                    "destination": state.output.destination_for(state.is_host_admin(request)),
                }
            raise HTTPException(status_code=415, detail=str(exc)) from exc


def sweep_posters(max_age=_POSTER_MAX_AGE):
    """Drop posters nothing has asked for in a month. Internal paths only."""
    folder = _thumb_dir()
    if not folder.is_dir():
        return 0
    cutoff = time.time() - max_age
    dropped = 0
    try:
        items = list(folder.iterdir())
    except OSError:
        return 0
    for item in items:
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                if fs_boundary.safe_internal_unlink(item):
                    dropped += 1
        except OSError:
            continue
    return dropped
