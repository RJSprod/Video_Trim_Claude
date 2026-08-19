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

# Media Transfer accepts stills as well as video. Centralised here rather than
# trusting the browser's accept= attribute, which is a hint to the file picker
# and not something the server may rely on.
IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".bmp", ".tif", ".tiff", ".avif", ".dng",
}

TRANSFERABLE_SUFFIXES = VIDEO_SUFFIXES | IMAGE_SUFFIXES

_CHUNK = 512 * 1024

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

mimetypes.add_type("video/mp4", ".m4v")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/mp2t", ".ts")


class MediaRegistry:
    """Maps unguessable tokens to absolute paths, scoped to one session each.

    Tokens used to be deduplicated by path and shared process-wide, which made
    every open file readable by every client for the life of the process: a
    remote user holding one could read anything a host-local user had ever
    opened, quietly undoing the separation between browsing and administering.

    So an entry now records who it belongs to and what capability minted it, the
    same path opened by two sessions yields two entries, and ending a session
    revokes its tokens. Unguessability is a nice property, not the authorization.
    """

    # What a token was created under, so a browse token cannot be replayed as
    # something more privileged.
    CAPABILITIES = ("host_browse", "upload", "proxy", "saved_output", "library")

    def __init__(self):
        self._lock = threading.Lock()
        self._by_token = {}

    def add(self, path, kind="source", session_id="", capability="upload"):
        resolved = Path(path).resolve()
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._by_token[token] = {
                "path": resolved,
                "kind": kind,
                "session_id": str(session_id or ""),
                "capability": str(capability),
                "added": time.time(),
            }
        return token

    def _owned_entry(self, token, session_id):
        with self._lock:
            entry = self._by_token.get(str(token))
        if not entry:
            return None
        # An entry with no owner is a server-side internal use (the cache sweep's
        # keep-list); it is never reachable from a request.
        if not entry["session_id"] or entry["session_id"] != str(session_id or ""):
            return None
        return entry

    def path_for(self, token, session_id):
        entry = self._owned_entry(token, session_id)
        if entry is None:
            return None
        path = entry["path"]
        return path if path.is_file() else None

    def entry_for(self, token, session_id):
        entry = self._owned_entry(token, session_id)
        return dict(entry) if entry else None

    def revoke_session(self, session_id):
        """Drop every token a session held. Called when the session ends."""
        session_id = str(session_id or "")
        if not session_id:
            return 0
        with self._lock:
            doomed = [
                token for token, entry in self._by_token.items()
                if entry["session_id"] == session_id
            ]
            for token in doomed:
                self._by_token.pop(token, None)
        return len(doomed)

    def known_paths(self):
        """Every path handed out so far — what the cache sweep must not delete."""
        with self._lock:
            return [entry["path"] for entry in self._by_token.values()]


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
