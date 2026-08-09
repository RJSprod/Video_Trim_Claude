"""Handing local files to the browser.

Two jobs. A registry, so the page refers to media by opaque token rather than by
path — nothing on disk is reachable over HTTP unless it was opened through the
UI. And a Range-aware reader, because ``<video>`` seeking *is* byte-range
requests: serve the whole file with a 200 and the scrubber stops working past
whatever is already buffered.
"""

import mimetypes
import re
import secrets
import threading
import time
from pathlib import Path

from starlette.responses import Response, StreamingResponse

# Video containers the desktop app opens, mirrored so the file browser lists
# the same things. Whether the *browser* can decode one is a separate question
# the player answers at runtime (see the proxy transcode).
VIDEO_SUFFIXES = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm",
    ".flv", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp",
}

_CHUNK = 512 * 1024

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

mimetypes.add_type("video/mp4", ".m4v")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/mp2t", ".ts")


class MediaRegistry:
    """Maps unguessable tokens to absolute paths, for the life of the process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_token = {}
        self._by_key = {}

    def add(self, path, kind="source"):
        resolved = Path(path).resolve()
        key = (str(resolved), kind)
        with self._lock:
            existing = self._by_key.get(key)
            if existing:
                return existing
            token = secrets.token_urlsafe(18)
            self._by_token[token] = {"path": resolved, "kind": kind, "added": time.time()}
            self._by_key[key] = token
            return token

    def path_for(self, token):
        with self._lock:
            entry = self._by_token.get(str(token))
        if not entry:
            return None
        path = entry["path"]
        return path if path.is_file() else None

    def entry_for(self, token):
        with self._lock:
            return dict(self._by_token.get(str(token)) or {}) or None


def content_type_for(path):
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "video/mp4"


def _parse_range(header, size):
    """Resolve a Range header to an inclusive ``(start, end)``.

    Returns None for "no range asked", or False for a range outside the file,
    which has to become a 416 rather than a silent full-file response.
    """
    if not header:
        return None
    match = _RANGE_RE.fullmatch(header.strip())
    if not match:
        return None

    raw_start, raw_end = match.group(1), match.group(2)
    if not raw_start and not raw_end:
        return None

    if not raw_start:  # bytes=-500 → the final 500 bytes
        length = int(raw_end)
        if length <= 0:
            return False
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
        end = min(end, size - 1)

    if start >= size or start > end:
        return False
    return start, end


def _iter_file(path, start, end):
    remaining = end - start + 1
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def file_response(path, range_header=None, head_only=False, download_name=None):
    """Serve ``path`` honouring Range, so seeking a large file stays instant."""
    path = Path(path)
    size = path.stat().st_size
    headers = {
        "Accept-Ranges": "bytes",
        # Media is immutable for a token's lifetime, but a Desktop file can be
        # replaced under us, so let the browser revalidate instead of caching.
        "Cache-Control": "no-cache",
    }
    if download_name:
        safe = download_name.replace('"', "")
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    media_type = content_type_for(path)

    span = _parse_range(range_header, size)

    if span is False:
        return Response(
            status_code=416,
            headers={**headers, "Content-Range": f"bytes */{size}"},
        )

    if span is None:
        start, end, status = 0, size - 1, 200
    else:
        start, end = span
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    length = end - start + 1 if size else 0
    headers["Content-Length"] = str(length)

    if head_only or length == 0:
        return Response(status_code=status, headers=headers, media_type=media_type)

    return StreamingResponse(
        _iter_file(path, start, end),
        status_code=status,
        headers=headers,
        media_type=media_type,
    )
