"""The one place this application is allowed to touch the host filesystem.

Everything outside the install directory is treated as somebody else's data. The
app may enumerate names, open a chosen media file read-only, and *create* brand
new files in a single directory the host picked. There is no external delete,
overwrite, truncate, rename, move or chmod capability anywhere in this module's
public surface, so feature code cannot reach one.

The single exception lives in :func:`commit_new_file`'s failure path and is not
exported: if the copy dies part-way, the gateway may unlink the file *it just
created* — but only while it can still prove authorship by holding the creating
descriptor and matching st_dev/st_ino. Immutability attaches at publication, not
at creation, so removing a file that never existed before this run says nothing
about pre-existing host data.

Vocabulary used throughout:

    INSTALL ROOT   the resolved directory containing one_click.py
    INTERNAL       ROOT itself, or anything resolving inside it
    EXTERNAL       everything else
    OUTPUT ROOT    the one existing external directory the host chose
    PUBLISHED      bytes streamed, flushed, fsynced, handle closed successfully
"""

import errno
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path

# Resolved once, with symlinks followed, and never recomputed from an
# unresolved value. Every containment test below compares resolved paths.
INSTALL_ROOT = Path(__file__).resolve().parents[2]

# Internal zones. The output root may not equal, contain or be contained by any
# of them — see validate_output_root().
DATA_DIR = INSTALL_ROOT / "data"
CACHE_DIR = INSTALL_ROOT / "cache"
LOGS_DIR = INSTALL_ROOT / "logs"
VENV_DIR = INSTALL_ROOT / "venv"

_PROTECTED_ZONES = (VENV_DIR, DATA_DIR, CACHE_DIR, LOGS_DIR)

# POSIX component limit; Windows is 255 per component too.
MAX_BASENAME = 255

_CONTROL_CHARS = "".join(chr(code) for code in range(0x00, 0x20)) + "\x7f"
_SEPARATORS = "/\\"

# CON.mp4 still opens the console device, so the check is against the stem.
_RESERVED_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{digit}" for digit in range(1, 10)]
    + [f"LPT{digit}" for digit in range(1, 10)]
)

_UNSAFE_RE = re.compile(f"[{re.escape(_SEPARATORS + _CONTROL_CHARS)}:]")


class FilesystemPolicyError(Exception):
    """Base class for a refusal by this module. Messages are fit to show a host."""


class UnsafeNameError(FilesystemPolicyError):
    """A requested basename is rejected outright rather than rewritten."""


class OutputRootError(FilesystemPolicyError):
    """The configured save location is missing, unusable, or overlaps the install."""


class ExternalReadError(FilesystemPolicyError):
    """A path was offered for reading that this app will not open."""


class CommitDenied(FilesystemPolicyError):
    """Authorization was withdrawn before the external create could happen."""


# --- containment -------------------------------------------------------------
def _resolved(path):
    """Fully resolve ``path``. Missing components resolve without erroring."""
    return Path(path).expanduser().resolve()


def is_internal(path):
    """True when ``path`` resolves to the install root or something inside it."""
    try:
        target = _resolved(path)
    except (OSError, ValueError, RuntimeError):
        return False
    if target == INSTALL_ROOT:
        return True
    return INSTALL_ROOT in target.parents


def require_internal(path, what="path"):
    """Return the resolved path, or raise if it is not inside the install root."""
    target = _resolved(path)
    if not is_internal(target):
        raise FilesystemPolicyError(
            f"Refusing to treat an external {what} as internal."
        )
    return target


def _overlaps(candidate, other):
    """True when either path is the other, or contains it."""
    return (
        candidate == other
        or other in candidate.parents
        or candidate in other.parents
    )


# --- external reads ----------------------------------------------------------
def validate_external_read(path):
    """Resolve a file the user selected, refusing anything that is not a file.

    Reading is the only thing we ever do with it: no mode here is a write mode,
    and callers open it ``"rb"``. Directories, devices, fifos and sockets are
    refused because handing one to ffmpeg is a way to make it block forever or
    read something nobody chose.
    """
    target = _resolved(path)
    try:
        info = target.lstat()
    except OSError as exc:
        raise ExternalReadError(f"{target.name} could not be read.") from exc
    if stat.S_ISLNK(info.st_mode):
        # Follow it once, deliberately, and judge the destination.
        try:
            info = target.stat()
        except OSError as exc:
            raise ExternalReadError(f"{target.name} could not be read.") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ExternalReadError(f"{target.name} is not a regular file.")
    return target


# --- output root -------------------------------------------------------------
def validate_output_root(path):
    """Resolve and vet the save location. Never creates it.

    The zone separation test is the important half. If the save location
    overlapped the install directory, the app's own internal cleanup — the cache
    sweep, a cancelled job's partial removal, safe_internal_rmtree — would become
    authorized to delete published output. So an overlapping directory is refused
    both when the host picks it and again on every single commit, because a mount
    or a link can create an overlap that did not exist at selection time.
    """
    if not path or not str(path).strip():
        raise OutputRootError("No save location has been chosen yet.")

    target = _resolved(path)

    if not target.exists():
        raise OutputRootError(
            f"The save location {target} does not exist. Choose an existing folder — "
            "Video Trim never creates one."
        )
    if not target.is_dir():
        raise OutputRootError(f"The save location {target} is not a folder.")

    if _overlaps(target, INSTALL_ROOT):
        raise OutputRootError(
            "That folder is inside the Video Trim installation. Choose a folder "
            "outside it, so the app's own cleanup can never touch your saved files."
        )
    for zone in _PROTECTED_ZONES:
        try:
            resolved_zone = zone.resolve()
        except (OSError, ValueError, RuntimeError):
            resolved_zone = zone
        if _overlaps(target, resolved_zone):
            raise OutputRootError(
                "That folder is inside the Video Trim installation. Choose a folder "
                "outside it, so the app's own cleanup can never touch your saved files."
            )

    return target


def output_root_is_writable(path):
    """Advisory only. Deliberately does not create a probe file.

    A "can I write here?" test file would need deleting afterwards, and this app
    has no external delete. So we ask the OS and accept that the real answer
    arrives when the exclusive create either works or does not.
    """
    try:
        return os.access(str(path), os.W_OK | os.X_OK)
    except OSError:
        return False


# --- names -------------------------------------------------------------------
def sanitize_basename(name):
    """Return ``name`` unchanged, or raise. Never rewrites into something else.

    Silent rewriting is how two different requested names end up colliding on one
    real file, so every rule here rejects. The colon is not cosmetic: on NTFS,
    creating ``photo.jpg:evil`` with CREATE_NEW *succeeds* when ``photo.jpg``
    exists, attaching an alternate data stream to it and changing its mtime —
    a modification of a pre-existing external file straight through the approved
    gateway.
    """
    if name is None:
        raise UnsafeNameError("A filename is required.")
    text = str(name)

    if not text or not text.strip():
        raise UnsafeNameError("That filename is empty.")
    if text in (".", ".."):
        raise UnsafeNameError("That filename is a directory reference.")
    if len(text) > MAX_BASENAME:
        raise UnsafeNameError("That filename is too long.")

    match = _UNSAFE_RE.search(text)
    if match:
        character = match.group(0)
        if character in _SEPARATORS:
            raise UnsafeNameError("A filename may not contain a path separator.")
        if character == ":":
            raise UnsafeNameError("A filename may not contain a colon.")
        raise UnsafeNameError("A filename may not contain control characters.")

    if text != text.rstrip(" ."):
        # Windows strips these silently, so "a." and "a" would become one file.
        raise UnsafeNameError("A filename may not end with a space or a dot.")
    if text.lstrip(" ") != text:
        raise UnsafeNameError("A filename may not begin with a space.")

    stem = text.split(".", 1)[0]
    if stem.upper() in _RESERVED_DEVICE_NAMES:
        raise UnsafeNameError(f"{stem} is a reserved device name on Windows.")

    return text


def is_safe_basename(name):
    try:
        sanitize_basename(name)
        return True
    except UnsafeNameError:
        return False


class CollisionPolicy:
    """How a feature reacts when its chosen name is already taken."""

    SKIP_BY_NAME = "skip_by_name"        # Media Transfer: never invent a variant
    UNIQUE_NEW_NAME = "unique_new_name"  # generated output: " (2)", " (3)", …


_MAX_NAME_ATTEMPTS = 40


def choose_unused_output_name(basename, policy):
    """Yield candidate basenames in the order the gateway should try them.

    Only a generator of *candidates*: existence is never consulted here. The
    exclusive create is the authority, because anything that checks first and
    opens second has a window in which the answer changes.
    """
    first = sanitize_basename(basename)
    yield first
    if policy != CollisionPolicy.UNIQUE_NEW_NAME:
        return

    suffix = Path(first).suffix
    stem = first[: len(first) - len(suffix)] if suffix else first
    for index in range(2, _MAX_NAME_ATTEMPTS):
        candidate = f"{stem} ({index}){suffix}"
        if is_safe_basename(candidate):
            yield candidate
    stamped = f"{stem} ({int(time.time())}){suffix}"
    if is_safe_basename(stamped):
        yield stamped


# --- internal-only mutation --------------------------------------------------
def safe_internal_unlink(path):
    """Delete a file, but only after proving it is inside the install root."""
    target = require_internal(path, what="file")
    try:
        if target.is_file():
            target.unlink()
            return True
    except OSError:
        pass
    return False


def safe_internal_rmtree(path):
    """Remove a directory tree, but only inside the install root."""
    target = require_internal(path, what="directory")
    if target == INSTALL_ROOT:
        raise FilesystemPolicyError("Refusing to remove the install root itself.")
    shutil.rmtree(target, ignore_errors=True)
    return True


def ensure_internal_dir(path, mode=0o700):
    """Create an internal working directory. Refuses external paths."""
    target = _resolved(path)
    if not is_internal(target):
        raise FilesystemPolicyError(
            "Refusing to create a directory outside the install root."
        )
    target.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            target.chmod(mode)
        except OSError:
            pass
    return target


# --- the exclusive-create gateway --------------------------------------------
class CommitResult:
    """What happened to one attempted external create.

    ``status`` is one of:
        published        a brand-new file exists and will never be touched again
        already_exists   the name was taken; nothing was written
        possible_partial a file this run created could not be cleaned up
    """

    def __init__(self, status, basename="", path=None, detail=""):
        self.status = status
        self.basename = basename
        self.path = path
        self.detail = detail

    @property
    def published(self):
        return self.status == "published"

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<CommitResult {self.status} {self.basename!r}>"


_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", set())

_COPY_CHUNK = 1024 * 1024


class _CreatedFile:
    """A file this process just created, and the evidence that it did.

    The descriptor is held for the whole copy on purpose. It is what lets the
    failure path prove that the thing it is about to unlink is the thing it made
    a moment ago, rather than whatever now happens to answer to that name.
    """

    def __init__(self, fd, basename, path, dir_fd=None):
        self.fd = fd
        self.basename = basename
        self.path = path
        self.dir_fd = dir_fd
        self.published = False
        self.pending_id = None
        info = os.fstat(fd)
        self.st_dev = info.st_dev
        self.st_ino = info.st_ino
        self.volume_id = _windows_file_id(fd)

    def still_ours(self):
        """True when the open descriptor still names the file we created."""
        try:
            info = os.fstat(self.fd)
        except OSError:
            return False
        if os.name == "nt":
            current = _windows_file_id(self.fd)
            return current is not None and current == self.volume_id
        return info.st_dev == self.st_dev and info.st_ino == self.st_ino


def _windows_file_id(fd):
    """(volume serial, file index) for a handle, or None off Windows."""
    if os.name != "nt":  # pragma: no cover - POSIX
        return None
    try:  # pragma: no cover - Windows only
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD),
                        ("dwHighDateTime", wintypes.DWORD)]

        class _INFO(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", _FILETIME),
                ("ftLastAccessTime", _FILETIME),
                ("ftLastWriteTime", _FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        handle = msvcrt.get_osfhandle(fd)
        info = _INFO()
        if not ctypes.windll.kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(handle), ctypes.byref(info)
        ):
            return None
        return (
            info.dwVolumeSerialNumber,
            (info.nFileIndexHigh << 32) | info.nFileIndexLow,
        )
    except Exception:
        return None


def _create_exclusive_posix(root_fd, basename):
    """O_CREAT|O_EXCL relative to a directory handle.

    POSIX already fails this call when the final component is a symlink, so the
    final component needs no extra care. What the dir_fd buys is the parent: the
    directory we validated and the directory we create in are the same inode,
    with no window in between for a swap.
    """
    return os.open(
        basename,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=root_fd,
    )


def _create_exclusive_windows(directory, basename):  # pragma: no cover - Windows only
    """CREATE_NEW with FILE_FLAG_OPEN_REPARSE_POINT against the validated path.

    os.open cannot take a dir_fd on Windows, so this keeps a smaller residual
    window between validating the directory and creating inside it. Documented
    platform limitation, not an oversight.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    GENERIC_WRITE = 0x40000000
    CREATE_NEW = 1
    FILE_ATTRIBUTE_NORMAL = 0x80
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(Path(directory) / basename),
        GENERIC_WRITE,
        0,
        None,
        CREATE_NEW,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or handle is None:
        code = ctypes.get_last_error() or ctypes.windll.kernel32.GetLastError()
        # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
        if code in (80, 183):
            raise FileExistsError(errno.EEXIST, "The file already exists.")
        raise OSError(0, f"CreateFileW failed with Windows error {code}")
    return msvcrt.open_osfhandle(handle, os.O_WRONLY)


def _unlink_created(created):
    """Remove a file this run created and never published. Five conditions.

    Called only from the commit failure path below; it is not exported, and
    feature code has no way to reach it.
    """
    if created.published:
        return False
    if created.fd is None:
        return False
    if not created.still_ours():
        return False
    try:
        if created.dir_fd is not None:
            os.unlink(created.basename, dir_fd=created.dir_fd)
        elif os.name == "nt":  # pragma: no cover - Windows only
            os.unlink(str(created.path))
        else:
            return False
        return True
    except OSError:
        return False


def commit_new_file(
    staged_path,
    desired_basename,
    output_root,
    collision_policy=CollisionPolicy.UNIQUE_NEW_NAME,
    journal=None,
    job_id="",
    authorize=None,
):
    """Create one brand-new file in the output root from a completed internal file.

    The order matters and is the same for every feature that saves anything:
    finish the work internally, re-check who is asking, re-resolve and re-vet the
    destination, then let an exclusive create be the only thing that decides
    whether the file is ours to write.

    ``authorize`` is called immediately before the create so a permission
    revoked during a long render is still honoured. ``journal`` records the
    intent to create, so a crash between creating and cleaning up leaves a note
    the host can act on rather than a mystery file.
    """
    staged = require_internal(staged_path, what="staged file")
    if not staged.is_file():
        raise FilesystemPolicyError("The staged file is missing or not a regular file.")

    if authorize is not None:
        allowed, why = authorize()
        if not allowed:
            raise CommitDenied(why or "You are not allowed to save files right now.")

    root = validate_output_root(output_root)

    size = staged.stat().st_size
    try:
        free = shutil.disk_usage(str(root)).free
    except OSError:
        free = None
    if free is not None and size > free:
        raise FilesystemPolicyError(
            "There is not enough free space in the save location for that file."
        )

    root_fd = None
    try:
        if _SUPPORTS_DIR_FD:
            root_fd = os.open(str(root), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            # Recorded so a swapped mount between here and the create is visible.
            anchor = os.fstat(root_fd)
            if not stat.S_ISDIR(anchor.st_mode):
                raise OutputRootError("The save location is no longer a folder.")

        created = None
        last_error = None
        for candidate in choose_unused_output_name(desired_basename, collision_policy):
            # Journalled before the create, so a crash in between still leaves a
            # note. Cleared by row id, never by name — an earlier run may have
            # journalled this same basename, and that row is the only warning
            # that a file with that name might be incomplete.
            pending_id = None
            if journal is not None:
                pending_id = journal.record_pending(str(root), candidate, job_id)
            try:
                if root_fd is not None:
                    fd = _create_exclusive_posix(root_fd, candidate)
                else:  # pragma: no cover - Windows only
                    fd = _create_exclusive_windows(root, candidate)
            except FileExistsError:
                if journal is not None:
                    journal.clear_pending(pending_id)
                if collision_policy == CollisionPolicy.SKIP_BY_NAME:
                    return CommitResult("already_exists", basename=candidate,
                                        path=root / candidate)
                continue
            except OSError as exc:
                if journal is not None:
                    journal.clear_pending(pending_id)
                last_error = exc
                break
            created = _CreatedFile(fd, candidate, root / candidate, dir_fd=root_fd)
            created.pending_id = pending_id
            break

        if created is None:
            if last_error is not None:
                raise FilesystemPolicyError(
                    f"Could not create a file in the save location: {last_error}"
                )
            raise FilesystemPolicyError(
                "Could not find an unused name in the save location."
            )

        return _stream_and_publish(created, staged, journal, root)
    finally:
        if root_fd is not None:
            try:
                os.close(root_fd)
            except OSError:
                pass


def _stream_and_publish(created, staged, journal, root):
    """Copy the staged bytes into the created file and publish it."""
    try:
        with open(staged, "rb") as source:
            while True:
                chunk = source.read(_COPY_CHUNK)
                if not chunk:
                    break
                offset = 0
                while offset < len(chunk):
                    offset += os.write(created.fd, chunk[offset:])
        try:
            os.fsync(created.fd)
        except (OSError, AttributeError):
            pass
        os.close(created.fd)
        created.fd = None
        created.published = True
        if journal is not None:
            journal.clear_pending(created.pending_id)
        return CommitResult("published", basename=created.basename, path=created.path)
    except Exception as exc:
        removed = _unlink_created(created)
        if created.fd is not None:
            try:
                os.close(created.fd)
            except OSError:
                pass
            created.fd = None
        if removed:
            if journal is not None:
                journal.clear_pending(created.pending_id)
            raise FilesystemPolicyError(f"Saving failed: {exc}") from exc
        # Could not prove authorship, so the file stays exactly as it is. It is
        # journalled instead, and reported to the host — never repaired.
        return CommitResult(
            "possible_partial",
            basename=created.basename,
            path=created.path,
            detail=str(exc),
        )


def describe_for(path, host_local, display_name="the host's save folder"):
    """A destination string safe for the session asking.

    Host-local sessions get the real path. Remote sessions get a label and never
    a path, drive letter or username-bearing string — in body text, toasts,
    tooltips, job labels, download banners and error details alike.
    """
    return str(path) if host_local else display_name


def platform_notes():
    """What this build can and cannot anchor, for the host to see in Settings."""
    return {
        "dir_fd": _SUPPORTS_DIR_FD,
        "platform": sys.platform,
        "install_root": str(INSTALL_ROOT),
    }
