"""The web application: an authenticated shell, with Video Trim as one tool.

Shape of the thing:

    /login                  the only unauthenticated page
    /                       Home — the tool launcher
    /tools/video-trim       the Gradio page and the hand-written player
    /tools/media-transfer   device-to-host media transfer
    /settings               host-local only
    /vt/api/...             the routes those views talk to
    /vt/media/<token>       a video, with byte ranges so scrubbing works
    /vt/saved/<token>       download something this app just created

Two rules run through all of it. Authentication is enforced once, by middleware
wrapped around the *outer* application, so routes this codebase does not author
are covered too. And every file that leaves this process does so through
``OutputService.publish`` — features render into ``cache/`` and never learn where
the save folder is.
"""

import inspect
import os
import string
import sys
import time
from pathlib import Path

import gradio as gr
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .. import ffmpeg_tools
from ..config.settings import SettingsService
from ..config.store import CommitJournal, Store
from ..ffmpeg_tools import FFmpegError
from ..naming import clip_name, frame_name
from ..paths import desktop_dir, sanitize
from ..security import fs_boundary
from ..security.auth import COOKIE_NAME, AuthError, AuthService, LoginThrottle
from ..security.auth import AccessRequestThrottle
from ..security.fs_boundary import CommitDenied, ExternalReadError
from ..security.middleware import AuthenticationMiddleware
from ..security.network import BrowseGuard, HostAdminGuard, client_ip, rate_limit_key
from ..security.write_policy import WritePolicy
from . import shell
from .admin import register_admin_routes
from .jobs import JobRegistry
from .media import VIDEO_SUFFIXES, MediaRegistry, file_response
from .output import CollisionPolicy, OutputService, OutputUnavailable
from .shell import TRANSFER_ROUTE, VIDEO_TRIM_ROUTE
from .transfer import register_transfer_routes

ASSETS = Path(__file__).resolve().parent / "assets"
ROOT = fs_boundary.INSTALL_ROOT

# Every scratch path this app uses lives under the install root, so the internal
# zone is freely mutable and the external zone is create-only. Those two zones
# may never overlap — see validate_output_root().
CACHE = fs_boundary.CACHE_DIR
UPLOAD_DIR = CACHE / "uploads"
PROXY_DIR = CACHE / "proxies"
GRADIO_TEMP = CACHE / "gradio"
CACHE_MAX_AGE = 24 * 3600

_UPLOAD_CHUNK = 1024 * 1024

# Leave this much disk free rather than filling it with an upload. ffmpeg still
# needs somewhere to write the clip afterwards.
UPLOAD_HEADROOM = 2 * 1024 ** 3

# Containers and codecs a browser will usually decode natively. Only used to
# warn early — the player still lets the browser decide, and offers a preview
# transcode when it can't.
_FRIENDLY_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".ogv"}
_FRIENDLY_CODECS = {"h264", "avc1", "vp8", "vp9", "av1", "theora"}

# Readable by JS on purpose: the page echoes it back in a header the middleware
# checks against the session's own token. A cross-site form cannot set a custom
# header, and the value is worthless without the HttpOnly session cookie.
CSRF_COOKIE = "vt_csrf"


def _asset_version():
    """Bust the browser cache whenever the front-end source changes."""
    stamps = []
    for name in ("player.js", "player.css", "shell.js", "shell.css",
                 "login.js", "login.css"):
        try:
            stamps.append(int((ASSETS / name).stat().st_mtime))
        except OSError:
            stamps.append(0)
    return str(max(stamps))


def _sweep_cache(keep=()):
    """Delete stale uploads and previews. Internal paths only.

    Anything still registered is kept however old it is: a long session must not
    have its source deleted from under it.
    """
    cutoff = time.time() - CACHE_MAX_AGE
    protected = {str(Path(path).resolve()) for path in keep}
    for folder in (UPLOAD_DIR, PROXY_DIR):
        fs_boundary.ensure_internal_dir(folder)
        try:
            items = list(folder.iterdir())
        except OSError:
            continue
        for item in items:
            try:
                if not item.is_file() or str(item.resolve()) in protected:
                    continue
                if item.stat().st_mtime < cutoff:
                    fs_boundary.safe_internal_unlink(item)
            except OSError:
                pass


class VideoTrimWeb:
    """Holds the pieces one server instance needs: security, storage, ffmpeg."""

    def __init__(self, store, allow_remote_files=False, proxy_height=720,
                 tunnel_active=False):
        self.store = store
        self.settings = SettingsService(store)
        self.journal = CommitJournal(store)
        self.registry = MediaRegistry()
        self.jobs = JobRegistry()

        # Ending a session revokes its media tokens, so a token cannot outlive
        # the authorization that produced it.
        self.auth = AuthService(store, on_session_end=self.registry.revoke_session)

        self.host_guard = HostAdminGuard(tunnel_active=tunnel_active)
        self.browse_guard = BrowseGuard(self.host_guard,
                                        allow_remote_files=allow_remote_files)
        self.write_policy = WritePolicy(store, self.host_guard)
        self.output = OutputService(self.settings, self.journal, self.registry)

        self.login_throttle = LoginThrottle()
        self.access_throttle = AccessRequestThrottle()

        self.allow_remote_files = bool(allow_remote_files)
        self.proxy_height = int(proxy_height)
        self.ffmpeg = ffmpeg_tools.find_ffmpeg()
        self.ffprobe = ffmpeg_tools.find_ffprobe()
        self.asset_version = _asset_version()

    # --- request context -----------------------------------------------------
    @staticmethod
    def session_for(request):
        """The session the middleware already resolved. Never re-parsed here."""
        return request.scope.get("vt_session")

    def require_session(self, request):
        session = self.session_for(request)
        if session is None:
            raise HTTPException(status_code=401, detail="Login required")
        return session

    def is_host_admin(self, request):
        return self.host_guard.is_host_request(request)

    def can_browse(self, request):
        return self.browse_guard.can_browse_host_paths(request)

    def guard_browse(self, request):
        """Reading host paths is a read capability, separate from being the host."""
        if self.can_browse(request):
            return
        raise HTTPException(
            status_code=403,
            detail=(
                "Opening files by path is limited to the machine running Video "
                "Trim. Send the video instead — it will be trimmed on that "
                "machine."
            ),
        )

    def require_ffmpeg(self):
        if not self.ffmpeg:
            raise HTTPException(status_code=503, detail=ffmpeg_tools.FFMPEG_HELP)
        return self.ffmpeg

    def require_write(self, request, session):
        decision = self.write_policy.evaluate(request, session)
        if not decision.allowed:
            raise HTTPException(status_code=403, detail=decision.reason)
        return decision

    # --- media ---------------------------------------------------------------
    def describe(self, path, session, request, kind="source", capability="upload"):
        """Probe a file and hand back what the player needs to load it.

        The absolute path is included only for a host-local session. A remote
        session gets the name and nothing that reveals the host's filesystem.
        """
        path = Path(path)
        info = ffmpeg_tools.probe_video(path, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        token = self.registry.add(path, kind=kind, session_id=session.id,
                                  capability=capability)
        friendly = (
            path.suffix.lower() in _FRIENDLY_SUFFIXES
            and (not info.get("codec") or info["codec"] in _FRIENDLY_CODECS)
        )
        host_local = self.is_host_admin(request)
        payload = {
            "token": token,
            "name": path.name,
            "duration_ms": info["duration_ms"],
            "fps": info["fps"],
            "width": info["width"],
            "height": info["height"],
            "codec": info.get("codec", ""),
            "media_url": f"/vt/media/{token}",
            "likely_playable": friendly,
            "destination": self.output.destination_for(host_local),
        }
        if host_local:
            payload["path"] = str(path)
        return payload


# --- shell routes ------------------------------------------------------------
def _register_shell_routes(app, state):
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_view(request: Request):
        # Already signed in? There is nothing to do here.
        if state.session_for(request) is not None:
            return _redirect("/")
        return HTMLResponse(shell.login_page(state.asset_version))

    @app.post("/api/login")
    async def login(request: Request):
        """The one unauthenticated state change in the application.

        Throttling counters live in memory: writing a row per attempt would let
        anyone who can reach the port drive unbounded disk writes just by
        guessing. Only aggregates reach SQLite, on transitions worth recording.
        """
        address = client_ip(request)
        key = rate_limit_key(address)

        cooling = state.login_throttle.blocked_for(key)
        if cooling:
            raise HTTPException(
                status_code=429,
                detail=f"Too many attempts. Try again in {max(1, cooling // 60)} minute(s).",
            )

        body = await _body(request)
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")

        # Reaching the login endpoint is what earns a permanent row. A passive
        # connection that never authenticates does not.
        state.store.ensure_ip(address)

        try:
            user = state.auth.verify(username, password)
        except AuthError as exc:
            _, just_locked = state.login_throttle.record_failure(key)
            state.store.flush_ip_aggregate(address, requests=1, failures=1)
            state.store.record_auth_event(address, "locked" if just_locked else "failure")
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        state.login_throttle.record_success(key)
        now = time.time()
        state.store.flush_ip_aggregate(address, requests=1, successes=1, last_success=now)
        state.store.record_auth_event(address, "success")

        host_local = state.is_host_admin(request)
        token, csrf = state.auth.start_session(
            user["id"], client_ip=address, host_local=host_local
        )
        response = JSONResponse({"status": "ok", "redirect": "/"})
        _set_session_cookies(request, response, token, csrf)
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        session = state.session_for(request)
        if session is not None:
            state.jobs.cancel_for_session(session.id)
            state.auth.end_session(session.token)
        response = JSONResponse({"status": "ok", "redirect": "/login"})
        response.delete_cookie(COOKIE_NAME, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def home_view():
        return HTMLResponse(shell.home_page(state.asset_version))

    @app.get(TRANSFER_ROUTE, response_class=HTMLResponse)
    def transfer_view():
        return HTMLResponse(shell.transfer_page(state.asset_version))

    @app.get(shell.SETTINGS_ROUTE, response_class=HTMLResponse)
    def settings_view(request: Request):
        # The card is hidden remotely, but hiding is a courtesy — this is the
        # part that actually refuses.
        if not state.is_host_admin(request):
            raise HTTPException(
                status_code=403,
                detail="Settings can only be opened on the machine running Video Trim.",
            )
        return HTMLResponse(shell.settings_page(state.asset_version))

    @app.get("/vt/api/capabilities")
    def capabilities(request: Request):
        session = state.require_session(request)
        host_admin = state.is_host_admin(request)
        decision = state.write_policy.evaluate(request, session)
        pending = 0
        partials = 0
        if host_admin:
            pending = sum(1 for row in state.store.list_ips() if row["access_requested_at"])
            partials = len(state.journal.survivors())
        return shell.capabilities(
            session,
            host_admin,
            state.can_browse(request),
            decision,
            state.settings,
            journal=state.journal,
            pending_requests=pending,
            partial_notices=partials,
        )


def _set_session_cookies(request, response, token, csrf):
    """HttpOnly session cookie, plus the readable CSRF companion.

    ``Secure`` is only set when the request actually arrived over HTTPS. Setting
    it on plain LAN HTTP would silently break login, and claiming a protection
    the transport does not provide is worse than saying so plainly — which the
    login page and Settings both do.
    """
    secure = request.url.scheme == "https"
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="strict", path="/", secure=secure
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, httponly=False, samesite="strict", path="/", secure=secure
    )


def _redirect(target):
    return Response(status_code=303, headers={"Location": target})


async def _body(request):
    """Accept JSON or a plain form post, so the login page works without JS."""
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            return await request.json()
        except Exception:
            return {}
    form = await request.form()
    return dict(form)


# --- Video Trim tool routes --------------------------------------------------
def _register_routes(app, state):
    @app.get("/vt/static/{name}")
    def static_asset(name: str):
        target = (ASSETS / name).resolve()
        if target.parent != ASSETS.resolve() or not target.is_file():
            raise HTTPException(status_code=404, detail="No such asset.")
        types = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
        media_type = types.get(target.suffix.lower(), "text/plain")
        return PlainTextResponse(
            target.read_text(encoding="utf-8"),
            media_type=f"{media_type}; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/vt/api/config")
    def config(request: Request):
        """What the player needs to decide how to present itself."""
        session = state.require_session(request)
        host_local = state.is_host_admin(request)
        decision = state.write_policy.evaluate(request, session)
        return {
            "ffmpeg": bool(state.ffmpeg),
            "ffmpeg_help": ffmpeg_tools.FFMPEG_HELP,
            "video_suffixes": sorted(VIDEO_SUFFIXES),
            "can_browse": state.can_browse(request),
            "is_host_admin": host_local,
            "can_write": decision.allowed,
            "write_reason": decision.reason,
            "can_request_access": decision.can_request,
            "destination": state.output.destination_for(host_local),
            "output_configured": state.output.is_configured(),
        }

    @app.api_route("/vt/media/{token}", methods=["GET", "HEAD"])
    def media(token: str, request: Request):
        """Serve a token's file — to the session that owns the token, only.

        Ownership rather than unguessability is the check. A token minted by a
        host-local browse must not be replayable by a remote session that
        happens to have obtained the string.
        """
        session = state.require_session(request)
        path = state.registry.path_for(token, session.id)
        if path is None:
            raise HTTPException(status_code=404, detail="That video is no longer open.")
        return file_response(
            path,
            range_header=request.headers.get("range"),
            head_only=request.method == "HEAD",
        )

    @app.api_route("/vt/saved/{token}", methods=["GET", "HEAD"])
    def saved(token: str, request: Request):
        session = state.require_session(request)
        entry = state.registry.entry_for(token, session.id) or {}
        path = state.registry.path_for(token, session.id)
        if path is None or entry.get("kind") != "saved":
            raise HTTPException(status_code=404, detail="That file is no longer available.")
        return file_response(
            path,
            range_header=request.headers.get("range"),
            head_only=request.method == "HEAD",
            download_name=path.name,
        )

    @app.post("/vt/api/open")
    async def open_path(request: Request):
        session = state.require_session(request)
        state.guard_browse(request)
        body = await request.json()
        raw = str(body.get("path") or "").strip().strip('"').strip("'")
        if not raw:
            raise HTTPException(status_code=400, detail="Give me a path to a video file.")

        path = Path(os.path.expandvars(raw)).expanduser()
        if not path.is_absolute():
            path = (Path.home() / path).resolve()
        if path.is_dir():
            raise HTTPException(status_code=400, detail="That is a folder, not a video file.")

        try:
            # Opened read-only, and never written back to: source media is
            # somebody else's file and stays byte-for-byte as it was.
            path = fs_boundary.validate_external_read(path)
        except ExternalReadError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        try:
            return state.describe(path, session, request, capability="host_browse")
        except FFmpegError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc

    @app.post("/vt/api/upload")
    async def upload(request: Request, name: str = ""):
        """Take a video from whichever browser is driving, local or not.

        Streamed straight to internal staging rather than buffered, because the
        remote case is a multi-gigabyte file from a phone.
        """
        session = state.require_session(request)
        state.require_write(request, session)

        multipart = "multipart/form-data" in (request.headers.get("content-type") or "")
        upload_file = None
        if multipart:
            form = await request.form()
            upload_file = form.get("file")
            if upload_file is None or not hasattr(upload_file, "read"):
                raise HTTPException(status_code=400, detail="No file was attached.")
            name = name or (upload_file.filename or "")

        safe = sanitize(Path(name or "upload").name) or "upload"
        suffix = Path(safe).suffix.lower()
        if not suffix:
            raise HTTPException(
                status_code=400,
                detail="That upload has no file extension, so its format is unknown.",
            )
        if suffix not in VIDEO_SUFFIXES:
            raise HTTPException(status_code=415, detail=f"{suffix} is not a video container.")

        fs_boundary.ensure_internal_dir(UPLOAD_DIR)
        _sweep_cache(keep=state.registry.known_paths())

        declared = request.headers.get("content-length")
        if declared and declared.isdigit():
            import shutil as _shutil
            free = _shutil.disk_usage(str(UPLOAD_DIR)).free
            if int(declared) + UPLOAD_HEADROOM > free:
                raise HTTPException(
                    status_code=507,
                    detail="There is not enough free space on the host for that file.",
                )

        # A fresh per-upload folder, so a staging name can never collide.
        job_id = state.output.new_job_id()
        staging = state.output.staging_dir("uploads", job_id)
        target = staging / safe
        try:
            with open(target, "wb") as handle:
                if multipart:
                    while chunk := await upload_file.read(_UPLOAD_CHUNK):
                        handle.write(chunk)
                    await upload_file.close()
                else:
                    async for chunk in request.stream():
                        handle.write(chunk)
            if target.stat().st_size == 0:
                raise HTTPException(status_code=400, detail="That upload was empty.")
        except HTTPException:
            state.output.discard_staging("uploads", job_id)
            raise
        except Exception as exc:
            state.output.discard_staging("uploads", job_id)
            raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc

        try:
            return state.describe(target, session, request, kind="upload",
                                  capability="upload")
        except FFmpegError as exc:
            state.output.discard_staging("uploads", job_id)
            raise HTTPException(status_code=415, detail=str(exc)) from exc

    @app.get("/vt/api/browse")
    def browse(request: Request, dir: str = ""):
        """List folders and videos, so the page has the desktop app's Open dialog.

        Names and safe metadata only, and no write side effects — this route
        cannot create, modify or remove anything it lists.
        """
        state.require_session(request)
        state.guard_browse(request)

        current = Path(os.path.expandvars(dir)).expanduser() if dir else Path.home()
        try:
            current = current.resolve()
        except OSError:
            current = Path.home()
        if not current.is_dir():
            current = Path.home()

        folders, files = [], []
        try:
            for item in sorted(current.iterdir(), key=lambda p: p.name.lower()):
                if item.name.startswith("."):
                    continue
                try:
                    if item.is_dir():
                        folders.append({"name": item.name, "path": str(item)})
                    elif item.suffix.lower() in VIDEO_SUFFIXES:
                        files.append({
                            "name": item.name,
                            "path": str(item),
                            "size": item.stat().st_size,
                        })
                except OSError:
                    continue
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="That folder is not readable.") from exc

        parent = str(current.parent) if current.parent != current else ""
        shortcuts = [{"name": "Home", "path": str(Path.home())}]
        desktop = desktop_dir()
        if desktop and desktop.is_dir():
            shortcuts.append({"name": "Desktop", "path": str(desktop)})
        for label in ("Videos", "Movies", "Downloads"):
            candidate = Path.home() / label
            if candidate.is_dir():
                shortcuts.append({"name": label, "path": str(candidate)})
        if sys.platform == "win32":
            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:\\")
                if drive.exists():
                    shortcuts.append({"name": f"{letter}:", "path": str(drive)})

        return {
            "dir": str(current),
            "parent": parent,
            "folders": folders,
            "files": files,
            "shortcuts": shortcuts,
        }

    @app.post("/vt/api/screenshot")
    async def screenshot(request: Request):
        """Full-resolution still: rendered internally, then published."""
        session = state.require_session(request)
        state.require_write(request, session)
        ffmpeg = state.require_ffmpeg()

        body = await request.json()
        source = state.registry.path_for(body.get("token"), session.id)
        if source is None:
            raise HTTPException(status_code=404, detail="That video is no longer open.")

        position = max(0, int(float(body.get("position_ms") or 0)))
        job_id = state.output.new_job_id()
        staging = state.output.staging_dir("exports", job_id)
        staged = staging / "frame.png"
        try:
            ffmpeg_tools.extract_frame(ffmpeg, source, staged, position)
        except FFmpegError as exc:
            state.output.discard_staging("exports", job_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except OSError as exc:
            state.output.discard_staging("exports", job_id)
            raise HTTPException(status_code=500, detail=f"Could not capture that frame: {exc}") from exc

        try:
            outcome = state.output.publish(
                staged,
                frame_name(source, position),
                CollisionPolicy.UNIQUE_NEW_NAME,
                authorize=state.write_policy.authorizer(request, session),
                job_id=job_id,
                session=session,
                host_local=state.is_host_admin(request),
            )
        except CommitDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except OutputUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            state.output.discard_staging("exports", job_id)
        return outcome.as_dict()

    @app.post("/vt/api/clip")
    async def clip(request: Request):
        """Start the A-B export. Cut from the original even when a proxy plays."""
        session = state.require_session(request)
        state.require_write(request, session)
        ffmpeg = state.require_ffmpeg()

        body = await request.json()
        source = state.registry.path_for(body.get("token"), session.id)
        if source is None:
            raise HTTPException(status_code=404, detail="That video is no longer open.")

        a_ms = int(float(body.get("a_ms")))
        b_ms = int(float(body.get("b_ms")))
        if b_ms - a_ms < 120:
            raise HTTPException(status_code=400, detail="That A-B range is too short to export.")
        if state.jobs.active("clip"):
            raise HTTPException(status_code=409, detail="An export is already running.")

        # ffmpeg renders into the app's own cache under a per-job folder. It
        # never names a file in the save folder, which is what closes the old
        # race between picking a free name and encoding over it.
        job_id = state.output.new_job_id()
        staging = state.output.staging_dir("exports", job_id)
        staged = staging / "clip.mp4"
        desired = clip_name(source, a_ms, b_ms)
        host_local = state.is_host_admin(request)
        authorize = state.write_policy.authorizer(request, session)

        def publish(finished):
            try:
                outcome = state.output.publish(
                    finished.target,
                    desired,
                    CollisionPolicy.UNIQUE_NEW_NAME,
                    authorize=authorize,
                    job_id=job_id,
                    session=session,
                    host_local=host_local,
                )
            finally:
                state.output.discard_staging("exports", job_id)
            return outcome.as_dict()

        job = state.jobs.start(
            "clip",
            f"Exporting clip → {desired}",
            b_ms - a_ms,
            ffmpeg_tools.clip_command(ffmpeg, source, staged, a_ms, b_ms),
            staged,
            on_success=publish,
            owner_session=session.id,
            owner_ip=session.client_ip,
        )
        return job.snapshot()

    @app.post("/vt/api/proxy")
    async def proxy(request: Request):
        """Transcode a browser-playable preview. Internal, and never an export source."""
        session = state.require_session(request)
        state.require_write(request, session)
        ffmpeg = state.require_ffmpeg()

        body = await request.json()
        token = body.get("token")
        source = state.registry.path_for(token, session.id)
        if source is None:
            raise HTTPException(status_code=404, detail="That video is no longer open.")

        info = ffmpeg_tools.probe_video(source, ffmpeg=state.ffmpeg, ffprobe=state.ffprobe)
        fs_boundary.ensure_internal_dir(PROXY_DIR)
        target = PROXY_DIR / f"{sanitize(source.stem)}_{str(token)[:8]}_preview.mp4"

        if target.is_file():
            if target.stat().st_size > 0:
                preview = state.registry.add(target, kind="proxy",
                                             session_id=session.id, capability="proxy")
                return {
                    "id": "cached",
                    "kind": "proxy",
                    "state": "done",
                    "percent": 100,
                    "proxy_url": f"/vt/media/{preview}",
                }
            # A zero-byte leftover would make ffmpeg's "-n" refuse to run.
            fs_boundary.safe_internal_unlink(target)

        def publish(finished):
            preview = state.registry.add(finished.target, kind="proxy",
                                         session_id=session.id, capability="proxy")
            return {"proxy_url": f"/vt/media/{preview}"}

        job = state.jobs.start(
            "proxy",
            f"Building a preview of {source.name}",
            info["duration_ms"],
            ffmpeg_tools.proxy_command(ffmpeg, source, target, state.proxy_height),
            target,
            on_success=publish,
            owner_session=session.id,
            owner_ip=session.client_ip,
        )
        return job.snapshot()

    @app.get("/vt/api/job/{job_id}")
    def job_status(job_id: str, request: Request):
        session = state.require_session(request)
        job = state.jobs.get(job_id)
        if job is None or (job.owner_session and job.owner_session != session.id):
            raise HTTPException(status_code=404, detail="No such job.")
        return job.snapshot()

    @app.post("/vt/api/job/{job_id}/cancel")
    def job_cancel(job_id: str, request: Request):
        session = state.require_session(request)
        job = state.jobs.get(job_id)
        if job is None or (job.owner_session and job.owner_session != session.id):
            raise HTTPException(status_code=404, detail="No such job.")
        job.cancel()
        return job.snapshot()


# --- the Gradio page ---------------------------------------------------------
PLAYER_HTML = """
<style>
  /* The stylesheet arrives asynchronously from /vt/static. Hide the player
     until it lands so nothing renders unstyled for a frame. The bootstrap adds
     vt-ready on the link's load event, with a timeout so a failed fetch cannot
     leave the UI invisible. */
  #vt-app { visibility: hidden; }
  html.vt-ready #vt-app { visibility: visible; }
</style>
<div id="vt-app" class="vt-app" data-state="empty">
  <div class="vt-stage" id="vt-stage" tabindex="0">
    <video id="vt-video" class="vt-video" playsinline preload="metadata"></video>

    <div class="vt-placeholder" id="vt-placeholder">
      <div class="vt-placeholder-mark" data-icon="play"></div>
      <p class="vt-placeholder-title" id="vt-placeholder-title">Drop a video here</p>
      <p class="vt-placeholder-hint">
        <button type="button" class="vt-upload" data-vt="pick">Choose a video…</button>
      </p>
      <p class="vt-placeholder-hint vt-placeholder-local" id="vt-placeholder-local">
        or <button type="button" class="vt-link" data-vt="browse">browse this machine</button>
        for a file already on it
      </p>
      <p class="vt-placeholder-note" id="vt-output-note"></p>
    </div>

    <div class="vt-flash vt-flash-left" id="vt-flash-left"><span>&laquo; 5s</span></div>
    <div class="vt-flash vt-flash-right" id="vt-flash-right"><span>5s &raquo;</span></div>
    <div class="vt-toast" id="vt-toast"></div>

    <div class="vt-panel" id="vt-panel">
      <div class="vt-scrub" id="vt-scrub" role="slider" aria-label="Seek"
           aria-valuemin="0" aria-valuenow="0" aria-valuemax="0" tabindex="-1">
        <div class="vt-track">
          <div class="vt-buffer" id="vt-buffer"></div>
          <div class="vt-ab-fill" id="vt-ab-fill"></div>
          <div class="vt-played" id="vt-played"></div>
          <div class="vt-mark vt-mark-a" id="vt-mark-a"><span>A</span></div>
          <div class="vt-mark vt-mark-b" id="vt-mark-b"><span>B</span></div>
          <div class="vt-handle" id="vt-handle"></div>
        </div>
      </div>

      <div class="vt-row">
        <div class="vt-time">
          <div class="vt-time-main"><span id="vt-pos">0:00.0</span><i>/</i><span id="vt-dur">0:00</span></div>
          <div class="vt-time-ab" id="vt-time-ab">no A-B range</div>
        </div>

        <div class="vt-buttons">
          <button type="button" class="vt-btn" data-vt="stop" data-icon="stop"
                  title="Stop — back to A (Home)"></button>
          <button type="button" class="vt-btn" data-vt="back5" data-icon="back5"
                  title="Back 5 seconds (&larr;)"></button>
          <button type="button" class="vt-btn" data-vt="prev-frame" data-icon="prevFrame"
                  title="Previous frame (,)"></button>
          <button type="button" class="vt-btn vt-btn-primary" data-vt="play" data-icon="play"
                  title="Play / pause (Space)"></button>
          <button type="button" class="vt-btn" data-vt="next-frame" data-icon="nextFrame"
                  title="Next frame (.)"></button>
          <button type="button" class="vt-btn" data-vt="fwd5" data-icon="fwd5"
                  title="Forward 5 seconds (&rarr;)"></button>
          <button type="button" class="vt-btn" data-vt="repeat" data-icon="repeat"
                  title="Repeat the A-B range (R)"></button>
          <button type="button" class="vt-btn vt-btn-ab" data-vt="marker"
                  title="Cycle the A-B markers (B)">A-B</button>
          <button type="button" class="vt-btn" data-vt="clip" data-icon="clip"
                  title="Save the A-B clip (C)" disabled></button>
          <button type="button" class="vt-btn" data-vt="screenshot" data-icon="camera"
                  title="Save this frame (S)"></button>
          <button type="button" class="vt-btn" data-vt="mute" data-icon="volumeOn"
                  title="Mute (M)"></button>
          <button type="button" class="vt-btn" data-vt="fullscreen" data-icon="fullscreen"
                  title="Fullscreen (F)"></button>
          <button type="button" class="vt-btn" data-vt="browse" data-icon="folder"
                  title="Open a video (O)"></button>
        </div>
      </div>
    </div>

    <div class="vt-sheet" id="vt-sheet" hidden>
      <div class="vt-sheet-card">
        <header>
          <span id="vt-sheet-dir">&nbsp;</span>
          <button type="button" class="vt-sheet-close" data-vt="sheet-close" title="Close">&times;</button>
        </header>
        <nav id="vt-sheet-shortcuts"></nav>
        <ul id="vt-sheet-list"></ul>
      </div>
    </div>
  </div>

  <div class="vt-saved" id="vt-saved" hidden>
    <span class="vt-saved-label">Saved to <code id="vt-saved-dir"></code> &mdash; download:</span>
    <ul id="vt-saved-list"></ul>
  </div>

  <input type="file" id="vt-file-input" accept="video/*" hidden />
</div>
"""

HELP_MARKDOWN = """
**Gestures** — single tap pins the controls up (tap the video again to dismiss).
Double tap the middle to play or pause, the left third for &minus;5s, the right
third for &plus;5s. While paused the controls never hide themselves.

**Keyboard** — `Space` play/pause &middot; `←`/`→` ∓5s &middot; `,`/`.` or
`Shift+←`/`→` ∓1 frame &middot; `↑`/`↓` volume &middot; `Home` stop (back to A)
&middot; `B` cycle A-B &middot; `R` repeat &middot; `M` mute &middot; `S`
screenshot &middot; `C` save the A-B clip &middot; `F`/`F11` fullscreen &middot;
`O` open &middot; `Esc` leave fullscreen.

**A-B looping** — first tap of **A-B** sets A, the second sets B, the third
clears both. Once both exist the range is the whole world: seeks and the ±5s
skips clamp inside it, **Stop** returns to A, and **Repeat** wraps B back to A.

**Saving** — both outputs are rendered by ffmpeg on the machine running this
server, into that machine's own working folder, and then copied into the save
folder the host chose. Existing files there are never replaced: a clip that would
collide gets a new name of its own. Clips are cut from the *original* file even
when a browser-friendly preview is what's playing.
"""


def _bootstrap_js(version):
    """Pull the player's CSS and JS in from /vt/static.

    Done at runtime rather than through Blocks(head=...) or launch(head=...)
    because those moved between Gradio 5 and 6; a load handler is stable in both.
    """
    return """
    () => {
      const v = "%s";
      const root = document.querySelector("gradio-app") || document.documentElement;
      root.classList.add("dark");
      document.documentElement.classList.add("dark");
      const reveal = () => document.documentElement.classList.add("vt-ready");
      const add = (id, tag, attrs) => {
        if (document.getElementById(id)) return false;
        const node = document.createElement(tag);
        node.id = id;
        Object.keys(attrs).forEach((key) => { node[key] = attrs[key]; });
        document.head.appendChild(node);
        return true;
      };
      add("vt-shell-css", "link", { rel: "stylesheet", href: "/vt/static/shell.css?v=" + v });
      // Brings the shared header behaviour (Home, Sign out) into the tool.
      add("vt-shell-js", "script", { src: "/vt/static/shell.js?v=" + v, defer: true });
      if (!add("vt-css", "link", {
            rel: "stylesheet", href: "/vt/static/player.css?v=" + v,
            onload: reveal, onerror: reveal })) {
        reveal();
      } else {
        // Never leave the player hidden because a stylesheet stalled.
        setTimeout(reveal, 4000);
      }
      if (!add("vt-js", "script", { src: "/vt/static/player.js?v=" + v })
          && window.VideoTrim) {
        window.VideoTrim.mount();
      }
      return [];
    }
    """ % version


def build_blocks(state):
    """The Video Trim tool's page. Every control hands off to the player through
    JS, so no Python callback sits between a click and the routes above."""
    with gr.Blocks(title="Video Trim", analytics_enabled=False, fill_width=True) as demo:
        gr.HTML(
            '<header class="vt-header vt-header-tool">'
            '<a class="vt-home-link" href="/" aria-label="Back to Home">'
            '<span class="vt-home-icon" aria-hidden="true"></span><span>Home</span></a>'
            '<span class="vt-header-title">Video Trim</span>'
            '<button type="button" class="vt-signout" id="vt-signout">Sign out</button>'
            "</header>"
            "<div id='vt-head'>"
            "<p>Mark a VLC-style A-B loop, watch it repeat, then export that exact "
            "span as a clip or grab a full-resolution still.</p>"
            "</div>"
        )

        # Hidden by the player for visitors who cannot read host paths — see
        # applyReach() in player.js.
        with gr.Row(equal_height=True, elem_id="vt-source-row"):
            path_box = gr.Textbox(
                label="Video on the host machine",
                placeholder="paste a path and press Enter",
                lines=1,
                scale=8,
                container=True,
            )
            open_btn = gr.Button("Open", variant="primary", scale=1, min_width=90)
            browse_btn = gr.Button("Browse…", scale=1, min_width=90)

        gr.HTML(PLAYER_HTML)

        if not state.ffmpeg:
            gr.Markdown(
                "> **ffmpeg was not found inside this installation.** Playback still "
                "works, but clips and stills cannot be written. Install it with "
                "`pip install imageio-ffmpeg` inside the venv and restart. Video Trim "
                "deliberately will not run an ffmpeg found on the system PATH."
            )

        with gr.Accordion("Controls, shortcuts and where files land", open=False):
            gr.Markdown(HELP_MARKDOWN)

        open_js = "(p) => { window.VideoTrim && window.VideoTrim.openPath(p); return []; }"
        open_btn.click(fn=None, inputs=[path_box], outputs=None, js=open_js)
        path_box.submit(fn=None, inputs=[path_box], outputs=None, js=open_js)
        browse_btn.click(
            fn=None,
            inputs=None,
            outputs=None,
            js="() => { window.VideoTrim && window.VideoTrim.openBrowser(); return []; }",
        )
        demo.load(fn=None, inputs=None, outputs=None, js=_bootstrap_js(state.asset_version))

    return demo


def _filtered(func, wanted):
    """Pass only the kwargs this Gradio version actually accepts."""
    allowed = inspect.signature(func).parameters
    return {key: value for key, value in wanted.items() if key in allowed}


def create_app(allow_remote_files=False, proxy_height=720, store=None,
               tunnel_active=False):
    """Build the application: FastAPI routes, the Gradio tool, then the guard."""
    # Pinned before Gradio is touched, so its own temp handling stays inside the
    # install root rather than landing in the system temp directory.
    fs_boundary.ensure_internal_dir(GRADIO_TEMP)
    os.environ["GRADIO_TEMP_DIR"] = str(GRADIO_TEMP)

    _sweep_cache()
    state = VideoTrimWeb(
        store or Store(),
        allow_remote_files=allow_remote_files,
        proxy_height=proxy_height,
        tunnel_active=tunnel_active,
    )
    state.auth.purge_expired()

    app = FastAPI(title="Video Trim", docs_url=None, redoc_url=None)

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        # The player shows `detail` verbatim in its toast, so keep it human —
        # and free of host paths, which the callers above are careful about.
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    # Registered before the Gradio mount so these are not swallowed by it.
    _register_shell_routes(app, state)
    _register_routes(app, state)
    register_transfer_routes(app, state)
    register_admin_routes(app, state)

    demo = build_blocks(state)
    app = gr.mount_gradio_app(
        app,
        demo,
        path=VIDEO_TRIM_ROUTE,
        **_filtered(gr.mount_gradio_app, {
            "ssr_mode": False,
            "show_error": True,
            # Uploads go through our own route; nothing needs Gradio's file access.
            "allowed_paths": [],
            "max_file_size": None,
        }),
    )

    # Applied to the OUTER app, after the mount, on purpose. Gradio contributes
    # routes this codebase does not author — /gradio_api/file/..., /upload,
    # /queue/join, /config, /info, theme assets — and wrapping the inner app
    # before mounting would leave every one of them reachable.
    guarded = AuthenticationMiddleware(app, state.auth, state.host_guard, state.store)
    guarded.state = app.state
    app.state.video_trim = state
    return guarded
