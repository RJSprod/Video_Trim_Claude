"""The web application: a Gradio page plus the routes it talks to.

Gradio owns the page — layout, theme, the source toolbar. The video player
itself is hand-written HTML/JS (``assets/player.js``) because the point of this
app is the A-B loop, and no stock component does clamped seeking, frame stepping
or marker rendering. That player speaks to the ``/vt`` routes defined here:

    /vt/media/<token>   the video, with byte ranges so scrubbing works
    /vt/api/...         open, browse, screenshot, clip, job polling
    /vt/saved/<token>   download something that was just written

Everything is opened by token, never by path, so the only files reachable over
HTTP are the ones the user pointed at.
"""

import inspect
import os
import shutil
import socket
import string
import sys
import time
from pathlib import Path

import gradio as gr
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import ffmpeg_tools
from ..ffmpeg_tools import FFmpegError
from ..naming import clip_name, frame_name
from ..paths import output_dir, sanitize, unique_path
from .jobs import JobRegistry
from .media import VIDEO_SUFFIXES, file_response

ASSETS = Path(__file__).resolve().parent / "assets"
ROOT = Path(__file__).resolve().parents[2]

# Uploads and preview transcodes are scratch, not output: they live here rather
# than on the Desktop, and get swept on start-up.
CACHE = ROOT / "cache"
UPLOAD_DIR = CACHE / "uploads"
PROXY_DIR = CACHE / "proxies"
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


def _asset_version():
    """Bust the browser cache whenever the player source changes."""
    stamps = []
    for name in ("player.js", "player.css"):
        try:
            stamps.append(int((ASSETS / name).stat().st_mtime))
        except OSError:
            stamps.append(0)
    return str(max(stamps))


_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})


def _own_addresses():
    """Every address that means "this machine", for the local-request check."""
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addresses.add(info[4][0])
    except (OSError, socket.gaierror):
        pass

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # reserved address; sends nothing
        addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    # IPv4-mapped form, which is how a dual-stack listener reports v4 peers.
    for address in list(addresses):
        if address.count(".") == 3:
            addresses.add(f"::ffff:{address}")
    return frozenset(addresses)


def _sweep_cache(keep=()):
    """Delete stale uploads and previews.

    Anything still registered is kept however old it is: a long session must not
    have its source deleted from under it.
    """
    cutoff = time.time() - CACHE_MAX_AGE
    protected = {str(Path(path).resolve()) for path in keep}
    for folder in (UPLOAD_DIR, PROXY_DIR):
        folder.mkdir(parents=True, exist_ok=True)
        try:
            items = list(folder.iterdir())
        except OSError:
            continue
        for item in items:
            try:
                if not item.is_file() or str(item.resolve()) in protected:
                    continue
                if item.stat().st_mtime < cutoff:
                    item.unlink()
            except OSError:
                pass


class VideoTrimWeb:
    """Holds the pieces one server instance needs: registries, ffmpeg, config."""

    def __init__(self, registry, allow_remote_files=False, proxy_height=720):
        self.registry = registry
        self.jobs = JobRegistry()
        self.allow_remote_files = bool(allow_remote_files)
        self.proxy_height = int(proxy_height)
        self.ffmpeg = ffmpeg_tools.find_ffmpeg()
        self.ffprobe = ffmpeg_tools.find_ffprobe()
        self.asset_version = _asset_version()
        self.local_addresses = _own_addresses()

    # --- helpers -------------------------------------------------------------
    def require_ffmpeg(self):
        if not self.ffmpeg:
            raise HTTPException(status_code=503, detail=ffmpeg_tools.FFMPEG_HELP)
        return self.ffmpeg

    def is_local(self, request):
        """True when the request came from the machine running this server.

        Loopback is the obvious case. The host's own LAN addresses count too:
        the banner prints those, so opening that URL *on the host* must not look
        like a stranger — otherwise the file browser breaks for the one person
        who is entitled to it.
        """
        if self.allow_remote_files:
            return True
        client = (request.client.host if request.client else "") or ""
        if client in _LOOPBACK:
            return True
        return client in self.local_addresses

    def guard_local(self, request):
        """Reading host paths stays a local-machine privilege.

        The WebUI itself is served to the whole network by default so that a
        phone can upload a video, but "open this path" and "list this folder"
        would hand that network the host's filesystem, so they do not travel.
        """
        if self.is_local(request):
            return
        raise HTTPException(
            status_code=403,
            detail=(
                "Opening files by path is limited to the machine running Video "
                "Trim. Upload the video instead — it will be trimmed on that "
                "machine and saved to its Desktop. (Start the server with "
                "--allow-remote-files to lift this.)"
            ),
        )

    def describe(self, path, kind="source"):
        """Probe a file and hand back everything the player needs to load it."""
        path = Path(path)
        info = ffmpeg_tools.probe_video(path, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        token = self.registry.add(path, kind=kind)
        friendly = (
            path.suffix.lower() in _FRIENDLY_SUFFIXES
            and (not info.get("codec") or info["codec"] in _FRIENDLY_CODECS)
        )
        return {
            "token": token,
            "name": path.name,
            "path": str(path),
            "duration_ms": info["duration_ms"],
            "fps": info["fps"],
            "width": info["width"],
            "height": info["height"],
            "codec": info.get("codec", ""),
            "media_url": f"/vt/media/{token}",
            "likely_playable": friendly,
            "output_dir": str(output_dir()),
        }

    def publish_saved(self, path):
        path = Path(path)
        token = self.registry.add(path, kind="saved")
        return {
            "saved_name": path.name,
            "saved_path": str(path),
            "saved_dir": str(path.parent),
            "download_url": f"/vt/saved/{token}",
        }


# --- routes ------------------------------------------------------------------
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
        # is_local decides which way the page presents itself: sitting at the
        # host you get the path box and the folder browser, and from anywhere
        # else you get upload, because that is all that will work.
        return {
            "output_dir": str(output_dir()),
            "ffmpeg": bool(state.ffmpeg),
            "ffmpeg_help": ffmpeg_tools.FFMPEG_HELP,
            "video_suffixes": sorted(VIDEO_SUFFIXES),
            "allow_remote_files": state.allow_remote_files,
            "is_local": state.is_local(request),
        }

    @app.api_route("/vt/media/{token}", methods=["GET", "HEAD"])
    def media(token: str, request: Request):
        path = state.registry.path_for(token)
        if path is None:
            raise HTTPException(status_code=404, detail="That video is no longer open.")
        return file_response(
            path,
            range_header=request.headers.get("range"),
            head_only=request.method == "HEAD",
        )

    @app.api_route("/vt/saved/{token}", methods=["GET", "HEAD"])
    def saved(token: str, request: Request):
        entry = state.registry.entry_for(token) or {}
        path = state.registry.path_for(token)
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
        state.guard_local(request)
        body = await request.json()
        raw = str(body.get("path") or "").strip().strip('"').strip("'")
        if not raw:
            raise HTTPException(status_code=400, detail="Give me a path to a video file.")

        path = Path(os.path.expandvars(raw)).expanduser()
        if not path.is_absolute():
            path = (Path.home() / path).resolve()
        if path.is_dir():
            raise HTTPException(status_code=400, detail=f"{path} is a folder, not a video file.")
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"{path} does not exist.")

        try:
            return state.describe(path)
        except FFmpegError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc

    @app.post("/vt/api/upload")
    async def upload(request: Request, name: str = ""):
        """Take a video from whichever browser is driving, local or not.

        This is the route a remote visitor uses, so it streams the body straight
        to disk rather than letting the framework buffer a multi-gigabyte file
        first. The page sends raw bytes with ?name=; a multipart form still works
        for anything hand-rolled, at the cost of that buffering.
        """
        multipart = "multipart/form-data" in (request.headers.get("content-type") or "")

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

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        _sweep_cache(keep=state.registry.known_paths())

        # Refuse before writing if it clearly will not fit, rather than filling
        # the disk and failing at the end of a long upload.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit():
            free = shutil.disk_usage(UPLOAD_DIR).free
            if int(declared) + UPLOAD_HEADROOM > free:
                raise HTTPException(
                    status_code=507,
                    detail=(
                        f"That file is {int(declared) / 1e9:.1f} GB and only "
                        f"{free / 1e9:.1f} GB is free on the machine running "
                        "Video Trim."
                    ),
                )

        target = unique_path(UPLOAD_DIR / safe)
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
            target.unlink(missing_ok=True)
            raise
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc

        try:
            return state.describe(target, kind="upload")
        except FFmpegError as exc:
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=415, detail=str(exc)) from exc

    @app.get("/vt/api/browse")
    def browse(request: Request, dir: str = ""):
        """List folders and videos, so the page has the desktop app's Open dialog."""
        state.guard_local(request)

        if dir:
            current = Path(os.path.expandvars(dir)).expanduser()
        else:
            current = output_dir()
        try:
            current = current.resolve()
        except OSError:
            current = Path.home()
        if not current.is_dir():
            current = Path.home()

        folders, files = [], []
        try:
            for item in sorted(current.iterdir(), key=lambda p: p.name.lower()):
                if item.name.startswith(".") and item.name != "..":
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
            raise HTTPException(status_code=403, detail=f"{current} is not readable.") from exc

        parent = str(current.parent) if current.parent != current else ""
        shortcuts = [{"name": "Desktop", "path": str(output_dir())},
                     {"name": "Home", "path": str(Path.home())}]
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
        """Full-resolution still, straight to the Desktop, exactly like the app."""
        ffmpeg = state.require_ffmpeg()
        body = await request.json()
        source = state.registry.path_for(body.get("token"))
        if source is None:
            raise HTTPException(status_code=404, detail="That video is no longer open.")

        position = max(0, int(float(body.get("position_ms") or 0)))
        target = unique_path(output_dir() / frame_name(source, position))
        try:
            ffmpeg_tools.extract_frame(ffmpeg, source, target, position)
        except FFmpegError as exc:
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not write {target}: {exc}") from exc
        return state.publish_saved(target)

    @app.post("/vt/api/clip")
    async def clip(request: Request):
        """Start the A-B export. Cut from the original even when a proxy plays."""
        ffmpeg = state.require_ffmpeg()
        body = await request.json()
        source = state.registry.path_for(body.get("token"))
        if source is None:
            raise HTTPException(status_code=404, detail="That video is no longer open.")

        a_ms = int(float(body.get("a_ms")))
        b_ms = int(float(body.get("b_ms")))
        if b_ms - a_ms < 120:
            raise HTTPException(status_code=400, detail="That A-B range is too short to export.")
        if state.jobs.active("clip"):
            raise HTTPException(status_code=409, detail="An export is already running.")

        target = unique_path(output_dir() / clip_name(source, a_ms, b_ms))
        job = state.jobs.start(
            "clip",
            f"Exporting clip → {target.name}",
            b_ms - a_ms,
            ffmpeg_tools.clip_command(ffmpeg, source, target, a_ms, b_ms),
            target,
            on_success=lambda finished: state.publish_saved(finished.target),
        )
        return job.snapshot()

    @app.post("/vt/api/proxy")
    async def proxy(request: Request):
        """Transcode a browser-playable preview for a codec the browser refused."""
        ffmpeg = state.require_ffmpeg()
        body = await request.json()
        token = body.get("token")
        source = state.registry.path_for(token)
        if source is None:
            raise HTTPException(status_code=404, detail="That video is no longer open.")

        info = ffmpeg_tools.probe_video(source, ffmpeg=state.ffmpeg, ffprobe=state.ffprobe)
        PROXY_DIR.mkdir(parents=True, exist_ok=True)
        target = PROXY_DIR / f"{sanitize(source.stem)}_{token[:8]}_preview.mp4"

        if target.is_file() and target.stat().st_size > 0:
            preview = state.registry.add(target, kind="proxy")
            return {
                "id": "cached",
                "kind": "proxy",
                "state": "done",
                "percent": 100,
                "proxy_url": f"/vt/media/{preview}",
            }

        def publish(finished):
            preview = state.registry.add(finished.target, kind="proxy")
            return {"proxy_url": f"/vt/media/{preview}"}

        job = state.jobs.start(
            "proxy",
            f"Building a preview of {source.name}",
            info["duration_ms"],
            ffmpeg_tools.proxy_command(ffmpeg, source, target, state.proxy_height),
            target,
            on_success=publish,
        )
        return job.snapshot()

    @app.get("/vt/api/job/{job_id}")
    def job_status(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job.")
        return job.snapshot()

    @app.post("/vt/api/job/{job_id}/cancel")
    def job_cancel(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
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
                  title="Save the A-B clip to the Desktop (C)" disabled></button>
          <button type="button" class="vt-btn" data-vt="screenshot" data-icon="camera"
                  title="Save this frame to the Desktop (S)"></button>
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

**Saving** — both outputs are written by ffmpeg on the machine running this
server, to that machine's Desktop, auto-named from the source and timecode and
never overwriting. Clips are cut from the *original* file even when a
browser-friendly preview is what's playing, so quality never comes from the
preview.
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
      if (!document.getElementById("vt-css")) {
        const link = document.createElement("link");
        link.id = "vt-css";
        link.rel = "stylesheet";
        link.href = "/vt/static/player.css?v=" + v;
        link.onload = reveal;
        link.onerror = reveal;
        document.head.appendChild(link);
        // Never leave the player hidden because a stylesheet stalled.
        setTimeout(reveal, 4000);
      } else {
        reveal();
      }
      if (!document.getElementById("vt-js")) {
        const script = document.createElement("script");
        script.id = "vt-js";
        script.src = "/vt/static/player.js?v=" + v;
        document.head.appendChild(script);
      } else if (window.VideoTrim) {
        window.VideoTrim.mount();
      }
      return [];
    }
    """ % version


def build_blocks(state):
    """The Gradio page. Every control hands off to the player through JS, so no
    Python callback sits between a click and the routes above."""
    with gr.Blocks(title="Video Trim", analytics_enabled=False, fill_width=True) as demo:
        gr.HTML(
            "<div id='vt-head'>"
            "<h1>Video Trim</h1>"
            "<p>Mark a VLC-style A-B loop, watch it repeat, then export that exact "
            "span as a clip or grab a full-resolution still — both straight to "
            f"<code>{output_dir()}</code> on the machine running this server.</p>"
            "</div>"
        )

        # Hidden by the player for visitors from other machines, who cannot use
        # either control — see applyReach() in player.js.
        with gr.Row(equal_height=True, elem_id="vt-source-row"):
            path_box = gr.Textbox(
                label="Video on the host machine",
                placeholder=r"C:\Users\you\Videos\clip.mp4   —   paste a path and press Enter",
                lines=1,
                scale=8,
                container=True,
            )
            open_btn = gr.Button("Open", variant="primary", scale=1, min_width=90)
            browse_btn = gr.Button("Browse…", scale=1, min_width=90)

        gr.HTML(PLAYER_HTML)

        if not state.ffmpeg:
            gr.Markdown(
                "> **ffmpeg was not found.** Playback still works, but clips and "
                "stills cannot be written. Install it with `pip install "
                "imageio-ffmpeg` inside the venv, or put `ffmpeg` on PATH, then "
                "restart."
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


def create_app(allow_remote_files=False, proxy_height=720):
    """Build the FastAPI app with the Gradio UI mounted at the root."""
    from .media import MediaRegistry

    _sweep_cache()
    state = VideoTrimWeb(
        MediaRegistry(),
        allow_remote_files=allow_remote_files,
        proxy_height=proxy_height,
    )

    app = FastAPI(title="Video Trim", docs_url=None, redoc_url=None)

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        # The player shows `detail` verbatim in its toast, so keep it human.
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    # Registered before the Gradio mount so /vt/* is not swallowed by it.
    _register_routes(app, state)

    demo = build_blocks(state)
    app = gr.mount_gradio_app(
        app,
        demo,
        path="/",
        **_filtered(gr.mount_gradio_app, {
            "ssr_mode": False,
            "show_error": True,
            # Uploads go through our own route; nothing needs Gradio's file access.
            "allowed_paths": [],
            "max_file_size": None,
        }),
    )
    app.state.video_trim = state
    return app
