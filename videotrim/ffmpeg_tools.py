"""ffmpeg discovery, probing and encoding.

Shared by both front ends, so nothing here may import Qt: the desktop app wraps
these in a QThread (``exporter.py``) while the web UI runs them on a plain
worker thread. Keeping the command building in one place is what guarantees a
clip cut from the browser is byte-for-byte the same job as one cut from the
desktop window.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Keep the console window from flashing up on Windows for every ffmpeg call.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

FFMPEG_HELP = (
    "ffmpeg is needed to cut clips but was not found.\n\n"
    "Install it with either:\n"
    "    pip install imageio-ffmpeg\n"
    "    winget install Gyan.FFmpeg\n\n"
    "…then restart the app."
)

# Used only when a file reports neither frame timings nor a frame rate.
FALLBACK_FPS = 25.0

_ROOT = Path(__file__).resolve().parent.parent


class FFmpegError(RuntimeError):
    """An ffmpeg invocation failed; the message is fit to show a user."""


def _exe(name):
    return f"{name}.exe" if sys.platform == "win32" else name


def _find_tool(name):
    """Locate a binary: alongside the app, on PATH, or pip-installed."""
    exe = _exe(name)
    for candidate in (_ROOT / exe, _ROOT / "ffmpeg" / exe, _ROOT / "ffmpeg" / "bin" / exe):
        if candidate.is_file():
            return str(candidate)

    found = shutil.which(name)
    if found:
        return found

    try:
        import imageio_ffmpeg
    except Exception:
        return None

    try:
        ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None
    if name == "ffmpeg":
        return str(ffmpeg)
    # imageio-ffmpeg ships ffmpeg only, but a system ffprobe often sits beside
    # whatever ffmpeg we ended up with.
    sibling = ffmpeg.parent / exe
    return str(sibling) if sibling.is_file() else None


def find_ffmpeg():
    return _find_tool("ffmpeg")


def find_ffprobe():
    return _find_tool("ffprobe")


def _run(command, timeout=60):
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        universal_newlines=True,
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
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


def _probe_with_ffprobe(ffprobe, path):
    result = _run([
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,duration,codec_name",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ])
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


def _probe_with_ffmpeg(ffmpeg, path):
    """Fall back to reading what ``ffmpeg -i`` prints about the file.

    imageio-ffmpeg ships ffmpeg without ffprobe, so on a fresh venv this is the
    path that actually runs.
    """
    # No output file: ffmpeg dumps the stream summary to stderr and exits 1.
    result = _run([ffmpeg, "-hide_banner", "-i", str(path)])
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
def clip_command(ffmpeg, source, target, a_ms, b_ms, progress=True):
    """The A-B trim. Re-encodes so the cut starts exactly on marker A."""
    duration_ms = max(1, int(b_ms) - int(a_ms))
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-y",
        # Input seeking plus re-encode: ffmpeg decodes from the preceding
        # keyframe and discards, so the cut lands exactly on the A marker.
        "-ss", f"{int(a_ms) / 1000.0:.3f}",
        "-i", str(source),
        "-t", f"{duration_ms / 1000.0:.3f}",
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
    ]
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
        "-y",
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
        "-y",
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


# --- running -----------------------------------------------------------------
def run_with_progress(command, total_ms, on_progress=None, cancelled=None):
    """Run an ffmpeg job, reporting 0-100 from its ``-progress`` stream.

    ``cancelled`` is polled between progress lines; returning true terminates
    ffmpeg. Raises FFmpegError with the last stderr line on failure.
    """
    total_ms = max(1, int(total_ms))
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            universal_newlines=True,
            errors="replace",
            creationflags=NO_WINDOW,
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
    """Write one full-resolution PNG. Raises FFmpegError if ffmpeg refuses."""
    result = _run(frame_command(ffmpeg, source, target, position_ms), timeout=120)
    if result.returncode != 0 or not Path(target).is_file():
        detail = (result.stderr or "").strip().splitlines()
        raise FFmpegError(f"ffmpeg failed: {detail[-1] if detail else result.returncode}")
    return Path(target)
