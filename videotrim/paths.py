"""Filesystem helpers: locating the user's real Desktop and naming outputs."""

import os
import re
import sys
from pathlib import Path

# KNOWNFOLDERID for the user's Desktop. Asking Windows for this (rather than
# assuming ~/Desktop) is what makes OneDrive-redirected Desktops work.
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Lets the WebUI write somewhere else when it runs on a host with no Desktop
# (a headless box, a container). Unset by default: saves go to the Desktop.
_OUTPUT_ENV = "VIDEOTRIM_OUTPUT_DIR"


def _known_folder(guid_str):
    """Ask Windows where a known folder really is. ``None`` off Windows."""
    if sys.platform != "win32":
        return None
    try:
        # Imported here rather than at module scope: ctypes.wintypes is a
        # Windows-only module, and this file is now shared with the web UI,
        # which runs on Linux and macOS too.
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


def output_dir():
    """Where saved clips and stills land: the Desktop, or the env override."""
    override = os.environ.get(_OUTPUT_ENV, "").strip()
    if override:
        target = Path(override).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        return target
    return desktop_dir()


def desktop_dir():
    """Best effort path to the user's Desktop, falling back to the home dir."""
    home = Path.home()
    candidates = [_known_folder(_FOLDERID_DESKTOP), _xdg_desktop()]
    candidates += [home / "Desktop", home / "OneDrive" / "Desktop"]
    for path in candidates:
        if path and path.is_dir():
            return path
    return home


def sanitize(name):
    """Strip characters Windows refuses in filenames."""
    cleaned = _ILLEGAL.sub("_", str(name)).strip(" .")
    return cleaned or "video"


def unique_path(path):
    """Return ``path``, or ``name (2).ext`` etc. if it already exists."""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for index in range(2, 1000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem} ({os_unique()}){suffix}"


def os_unique():
    import time

    return str(int(time.time()))
