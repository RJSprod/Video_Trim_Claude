"""Filesystem helpers: locating the real Windows Desktop and naming outputs."""

import ctypes
import re
import sys
from ctypes import wintypes
from pathlib import Path

# KNOWNFOLDERID for the user's Desktop. Asking Windows for this (rather than
# assuming ~/Desktop) is what makes OneDrive-redirected Desktops work.
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]


def _known_folder(guid_str):
    if sys.platform != "win32":
        return None
    try:
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


def desktop_dir():
    """Best effort path to the user's Desktop, falling back to the home dir."""
    candidates = [_known_folder(_FOLDERID_DESKTOP)]
    home = Path.home()
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
