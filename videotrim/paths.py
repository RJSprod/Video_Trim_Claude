"""Naming helpers. **Not a security boundary.**

This module used to decide where output went and whether a name was safe. Both
jobs moved to ``videotrim/security/fs_boundary.py``, which rejects unsafe names
outright instead of rewriting them and uses exclusive creation instead of
"check, then open". Nothing here may be relied on to keep a file inside the save
location.

``sanitize`` below still exists because generated names have to be *pleasant*
(a clip called ``holiday: day 2.mp4`` should become something a Windows drive
will accept), but the gateway re-checks whatever comes out of it and refuses
rather than trusting it.
"""

import os
import re
import sys
from pathlib import Path

# KNOWNFOLDERID for the user's Desktop. Asking Windows for this (rather than
# assuming ~/Desktop) is what makes OneDrive-redirected Desktops work. Kept for
# the Settings folder browser's shortcuts — it is a *starting point to browse*,
# never a place anything is written by default.
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _known_folder(guid_str):
    """Ask Windows where a known folder really is. ``None`` off Windows."""
    if sys.platform != "win32":
        return None
    try:
        # Imported here rather than at module scope: ctypes.wintypes is a
        # Windows-only module, and this file is shared with the web UI, which
        # runs on Linux and macOS too.
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_byte * 8),
            ]

        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
        guid = _GUID()
        if ole32.CLSIDFromString(ctypes.c_wchar_p(guid_str), ctypes.byref(guid)) != 0:
            return None
        out = ctypes.c_wchar_p()
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value) if out.value else None
        finally:
            ole32.CoTaskMemFree(out)
    except Exception:
        return None


def _xdg_desktop():
    """Read XDG_DESKTOP_DIR, which is how a localised Linux Desktop is found."""
    if sys.platform in ("win32", "darwin"):
        return None
    env = os.environ.get("XDG_DESKTOP_DIR")
    if env:
        return Path(os.path.expandvars(env)).expanduser()

    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    user_dirs = config / "user-dirs.dirs"
    try:
        text = user_dirs.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'^\s*XDG_DESKTOP_DIR\s*=\s*"?(.*?)"?\s*$', text, re.MULTILINE)
    if not match:
        return None
    raw = match.group(1).replace("$HOME", str(Path.home()))
    return Path(os.path.expandvars(raw)).expanduser() if raw else None


def desktop_dir():
    """Best effort path to the user's Desktop, for the browser's shortcut list.

    Never created, never written to, and never used as a fallback destination.
    If the host has not chosen a save location, saving fails and says so.
    """
    home = Path.home()
    candidates = [_known_folder(_FOLDERID_DESKTOP), _xdg_desktop()]
    candidates += [home / "Desktop", home / "OneDrive" / "Desktop"]
    for path in candidates:
        if path and path.is_dir():
            return path
    return home


def sanitize(name):
    """Make a generated name pleasant. Cosmetic only — the gateway still vets it."""
    cleaned = _ILLEGAL.sub("_", str(name)).strip(" .")
    return cleaned or "video"
