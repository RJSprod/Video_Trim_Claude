"""ffmpeg discovery, probing and encoding.

Shared by both front ends, so nothing here may import Qt: the desktop app wraps
these in a QThread (``exporter.py``) while the web UI runs them on a plain
worker thread. Keeping the command building in one place is what guarantees a
clip cut from the browser is byte-for-byte the same job as one cut from the
desktop window.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

from . import encoding
from .security.fs_boundary import INSTALL_ROOT, is_internal

# Keep the console window from flashing up on Windows for every ffmpeg call.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

FFMPEG_HELP = (
    "ffmpeg is needed to cut clips but was not found inside this installation.\n\n"
    "Install it into the venv:\n"
    "    pip install imageio-ffmpeg\n\n"
    "…or drop the binary in the app's own ffmpeg/ folder, then restart. Video "
    "Trim deliberately will not run an ffmpeg found on the system PATH."
)

# Used only when a file reports neither frame timings nor a frame rate.
FALLBACK_FPS = 25.0

_ROOT = INSTALL_ROOT

# The only protocols any invocation may use. This is not advisory: a crafted
# HLS, concat or matroska input can otherwise make ffmpeg read arbitrary host
# paths or issue outbound requests, turning the render pipeline into an
# exfiltration channel. It is a demuxer option, so it has to appear *before* the
# -i it applies to — placed after, it parses cleanly and does nothing.
PROTOCOL_WHITELIST = ("-protocol_whitelist", "file,pipe")


class FFmpegError(RuntimeError):
    """An ffmpeg invocation failed; the message is fit to show a user."""


class UntrustedExecutable(FFmpegError):
    """A binary was offered that does not live inside this installation."""


def _exe(name):
    return f"{name}.exe" if sys.platform == "win32" else name


def _find_tool(name):
    """Locate a binary that ships with this installation. Never searches PATH.

    Searching PATH would let anything earlier on it decide what this app
    executes. The imageio-ffmpeg fallback is acceptable only because it resolves
    inside the venv — and that is verified rather than assumed, because the
    import succeeding says nothing about where the binary ended up.
    """
    exe = _exe(name)
    for candidate in (_ROOT / exe, _ROOT / "ffmpeg" / exe, _ROOT / "ffmpeg" / "bin" / exe):
        if candidate.is_file() and is_internal(candidate):
            return str(candidate)

    try:
        import imageio_ffmpeg
    except Exception:
        return None

    try:
        ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None
    if name == "ffmpeg":
        return str(ffmpeg) if is_internal(ffmpeg) else None
    # imageio-ffmpeg ships ffmpeg only, but an ffprobe often sits beside it.
    sibling = ffmpeg.parent / exe
    if sibling.is_file() and is_internal(sibling):
        return str(sibling)
    return None


def find_ffmpeg():
    return _find_tool("ffmpeg")


def find_ffprobe():
    return _find_tool("ffprobe")


def verify_executable(path):
    """Re-check containment immediately before running. Returns the path.

    Deliberately checked here rather than only at discovery: the value could
    have been stored, passed around, or swapped since, and the moment before
    Popen is the only moment that matters.
    """
    if not path:
        raise FFmpegError(FFMPEG_HELP)
    resolved = Path(path).resolve()
    if not resolved.is_file() or not is_internal(resolved):
        raise UntrustedExecutable(
            "Refusing to run a media tool from outside the Video Trim installation."
        )
    return str(resolved)


def _run(command, timeout=60):
    """Run a child process. Argument list, no shell, no inherited stdin."""
    command = [verify_executable(command[0])] + [str(part) for part in command[1:]]
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        universal_newlines=True,
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
        shell=False,
    )


# --- probing -----------------------------------------------------------------
def _fraction(text):
    """Parse ffprobe's ``30000/1001`` style rates."""
    try:
        if "/" in str(text):
            num, den = str(text).split("/", 1)
            den = float(den)
            return float(num) / den if den else 0.0
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def probe_command(ffprobe, path):
    """ffprobe's argument list, with the protocol whitelist ahead of the input.

    The probe is the first thing that touches attacker-supplied bytes — an
    upload is probed before anything else looks at it — so this is the
    invocation that needs the restriction most, not least.
    """
    return [
        ffprobe,
        "-v", "error",
        *PROTOCOL_WHITELIST,
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,duration,codec_name",
        "-show_entries", "format=duration",
        "-of", "json",
        "-i", str(path),
    ]


def _probe_with_ffprobe(ffprobe, path):
    result = _run(probe_command(ffprobe, path))
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return None

    streams = data.get("streams") or []
    if not streams:
        return None
    stream = streams[0]

    fps = _fraction(stream.get("avg_frame_rate")) or _fraction(stream.get("r_frame_rate"))
    seconds = 0.0
    for source in (data.get("format", {}).get("duration"), stream.get("duration")):
        try:
            seconds = float(source)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            break

    return {
        "duration_ms": int(round(max(0.0, seconds) * 1000)),
        "fps": round(fps, 6) if fps > 0 else 0.0,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "codec": str(stream.get("codec_name") or "").lower(),
    }


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: (?P<rest>.*)")
_CODEC_RE = re.compile(r"([A-Za-z0-9_]+)")
_SIZE_RE = re.compile(r"(?<![\d.])(\d{2,5})x(\d{2,5})(?![\dx])")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s+fps")
_TBR_RE = re.compile(r"(\d+(?:\.\d+)?)\s+tbr")


def ffmpeg_probe_command(ffmpeg, path):
    """The ``ffmpeg -i`` fallback probe, whitelist first."""
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        *PROTOCOL_WHITELIST,
        "-i", str(path),
    ]


def _probe_with_ffmpeg(ffmpeg, path):
    """Fall back to reading what ``ffmpeg -i`` prints about the file.

    imageio-ffmpeg ships ffmpeg without ffprobe, so on a fresh venv this is the
    path that actually runs.
    """
    # No output file: ffmpeg dumps the stream summary to stderr and exits 1.
    result = _run(ffmpeg_probe_command(ffmpeg, path))
    text = f"{result.stderr}\n{result.stdout}"

    duration_ms = 0
    match = _DURATION_RE.search(text)
    if match:
        hours, mins, secs = int(match.group(1)), int(match.group(2)), float(match.group(3))
        duration_ms = int(round((hours * 3600 + mins * 60 + secs) * 1000))

    width = height = 0
    fps = 0.0
    codec = ""
    video = _VIDEO_RE.search(text)
    if video:
        rest = video.group("rest")
        name = _CODEC_RE.match(rest)
        if name:
            codec = name.group(1).lower()
        size = _SIZE_RE.search(rest)
        if size:
            width, height = int(size.group(1)), int(size.group(2))
        rate = _FPS_RE.search(rest) or _TBR_RE.search(rest)
        if rate:
            fps = float(rate.group(1))
    elif duration_ms == 0:
        return None

    return {
        "duration_ms": duration_ms,
        "fps": round(fps, 6),
        "width": width,
        "height": height,
        "codec": codec,
    }


def probe_video(path, ffmpeg=None, ffprobe=None):
    """Report duration, frame rate and pixel size, or raise FFmpegError.

    The frame rate matters: it is what the browser player steps by, so a file
    that reports nothing usable falls back to FALLBACK_FPS rather than making
    the step buttons do nothing.
    """
    path = Path(path)
    if not path.is_file():
        raise FFmpegError(f"{path} is not a file.")

    ffprobe = ffprobe or find_ffprobe()
    info = _probe_with_ffprobe(ffprobe, path) if ffprobe else None

    if not info or info["duration_ms"] <= 0 or info["fps"] <= 0:
        ffmpeg = ffmpeg or find_ffmpeg()
        fallback = _probe_with_ffmpeg(ffmpeg, path) if ffmpeg else None
        if fallback:
            info = info or fallback
            for key in ("duration_ms", "fps", "width", "height", "codec"):
                if not info.get(key):
                    info[key] = fallback.get(key) or (0 if key != "codec" else "")

    if not info:
        raise FFmpegError("That file could not be read — unsupported or damaged.")
    if info["duration_ms"] <= 0:
        raise FFmpegError("That file reports no duration — unsupported or damaged.")
    if info["fps"] <= 0:
        info["fps"] = FALLBACK_FPS
    return info


# --- commands ----------------------------------------------------------------
def clip_command(ffmpeg, source, target, a_ms, b_ms, progress=True, options=None):
    """The A-B trim. Re-encodes so the cut starts exactly on marker A.

    ``target`` is always a fresh path inside the app's own cache. ffmpeg never
    points at anything outside the installation, which is why ``-n`` here is
    belt-and-braces rather than the thing standing between a user's file and an
    overwrite — that job belongs to the exclusive-create gateway.

    ``options`` is what the player's gear menu chose: frame size, CRF, encoder
    preset, a frame-rate cap and the audio bitrate. It is normalized before it
    gets here, so every value below is a bounded number or one of a fixed set of
    words — none of it is a string that came from a browser.
    """
    duration_ms = max(1, int(b_ms) - int(a_ms))
    # Already vetted against the source by encoding.normalize(); bounded again
    # here so this builder is safe to call with whatever a caller hands it.
    settings = encoding.command_settings(options)
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-n",
        *PROTOCOL_WHITELIST,
        # Input seeking plus re-encode: ffmpeg decodes from the preceding
        # keyframe and discards, so the cut lands exactly on the A marker.
        "-ss", f"{int(a_ms) / 1000.0:.3f}",
        "-i", str(source),
        "-t", f"{duration_ms / 1000.0:.3f}",
        "-map", "0:v:0?",
    ]
    if settings["audio_kbps"]:
        command += ["-map", "0:a:0?"]

    scale = encoding.scale_filter(settings)
    if scale:
        command += ["-vf", scale]
    if settings["fps_cap"]:
        # A cap, never an increase: -r above the source rate would duplicate
        # frames and make the file bigger for nothing.
        command += ["-r", str(int(settings["fps_cap"]))]

    command += [
        "-c:v", "libx264",
        "-preset", str(settings["preset"]),
        "-crf", str(int(settings["crf"])),
        "-pix_fmt", "yuv420p",
    ]
    if settings["audio_kbps"]:
        command += ["-c:a", "aac", "-b:a", f"{int(settings['audio_kbps'])}k"]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart"]

    if progress:
        command += ["-progress", "pipe:1", "-nostats"]
    return command + [str(target)]


def frame_command(ffmpeg, source, target, position_ms):
    """A single full-resolution still at ``position_ms``.

    Seeking before ``-i`` is both fast and accurate in current ffmpeg: it jumps
    to the preceding keyframe then decodes forward to the exact timestamp, so
    the PNG is the frame the player was showing, at the source's own pixel size.
    """
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-n",
        *PROTOCOL_WHITELIST,
        "-ss", f"{max(0, int(position_ms)) / 1000.0:.3f}",
        "-i", str(source),
        "-frames:v", "1",
        "-update", "1",
        "-f", "image2",
        str(target),
    ]


def proxy_command(ffmpeg, source, target, height=720, progress=True):
    """A browser-playable H.264/AAC copy, used when a codec won't play natively.

    Only ever a preview: exports are always cut from the original file, so the
    proxy's quality never reaches anything that gets saved.
    """
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-n",
        *PROTOCOL_WHITELIST,
        "-i", str(source),
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        # Downscale only when the source is taller, and keep the height even.
        "-vf", f"scale=-2:'min({int(height)},ih)':flags=fast_bilinear",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "26",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "2",
        "-movflags", "+faststart",
    ]
    if progress:
        command += ["-progress", "pipe:1", "-nostats"]
    return command + [str(target)]


def poster_command(ffmpeg, source, target, position_ms=0, width=480):
    """One small JPEG for the file browser's icon views.

    Written into the app's own cache and served from there, so a folder of a
    hundred videos costs one decode each rather than one per page view. Same
    protocol whitelist as everything else: a poster is still ffmpeg opening a
    file somebody else wrote.
    """
    width = max(64, min(1280, int(width)))
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-n",
        *PROTOCOL_WHITELIST,
        "-ss", f"{max(0, int(position_ms)) / 1000.0:.3f}",
        "-i", str(source),
        "-frames:v", "1",
        "-vf", f"scale={width}:-2:flags=fast_bilinear",
        "-f", "image2",
        "-c:v", "mjpeg",
        "-q:v", "5",
        str(target),
    ]


def extract_poster(ffmpeg, source, target, position_ms=0, width=480):
    """Render a poster into an internal path, retrying at the first frame.

    A short clip seeked past its own end produces no output at all, which would
    leave the browser showing a broken tile for a perfectly good file. So the
    seek is a preference, not a requirement.
    """
    if not is_internal(target):
        raise FFmpegError("Refusing to render to a path outside the installation.")
    for position in (max(0, int(position_ms)), 0):
        result = _run(poster_command(ffmpeg, source, target, position, width), timeout=30)
        if result.returncode == 0 and Path(target).is_file():
            return Path(target)
        if position == 0:
            break
    raise FFmpegError("No frame could be read from that file.")


# --- running -----------------------------------------------------------------
def run_with_progress(command, total_ms, on_progress=None, cancelled=None):
    """Run an ffmpeg job, reporting 0-100 from its ``-progress`` stream.

    ``cancelled`` is polled between progress lines; returning true terminates
    ffmpeg. Raises FFmpegError with the last stderr line on failure.
    """
    total_ms = max(1, int(total_ms))
    # Containment is re-checked here, the last statement before the process
    # actually starts, rather than trusted from whenever the path was found.
    command = [verify_executable(command[0])] + [str(part) for part in command[1:]]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            universal_newlines=True,
            errors="replace",
            creationflags=NO_WINDOW,
            shell=False,
        )
    except Exception as exc:  # pragma: no cover - depends on local install
        raise FFmpegError(f"Could not start ffmpeg: {exc}") from exc

    stopped = False
    for line in process.stdout:
        if cancelled is not None and cancelled():
            stopped = True
            try:
                process.terminate()
            except Exception:
                pass
            break
        line = line.strip()
        if line.startswith("out_time_ms=") and on_progress is not None:
            try:
                done = int(line.split("=", 1)[1]) / 1000.0
            except ValueError:
                continue
            on_progress(int(max(0.0, min(100.0, done / total_ms * 100.0))))

    stderr = ""
    try:
        stderr = process.stderr.read() or ""
    except Exception:
        pass
    code = process.wait()

    if stopped:
        raise FFmpegError("Export cancelled.")
    if code != 0:
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else f"exit code {code}"
        raise FFmpegError(f"ffmpeg failed: {detail}")
    if on_progress is not None:
        on_progress(100)


def extract_frame(ffmpeg, source, target, position_ms):
    """Write one full-resolution PNG to an internal staging path.

    The containment check is not decoration: it is what guarantees no ffmpeg
    invocation anywhere in this codebase can name a file outside the install
    directory as its output.
    """
    if not is_internal(target):
        raise FFmpegError("Refusing to render to a path outside the installation.")
    result = _run(frame_command(ffmpeg, source, target, position_ms), timeout=120)
    if result.returncode != 0 or not Path(target).is_file():
        detail = (result.stderr or "").strip().splitlines()
        raise FFmpegError(f"ffmpeg failed: {detail[-1] if detail else result.returncode}")
    return Path(target)
