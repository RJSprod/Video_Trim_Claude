#!/usr/bin/env python3
"""Fail the build on any mutating filesystem or subprocess call we did not vet.

A manual "review every hit and categorise it" rots within two sprints, so this
is a script with a checked-in allowlist instead. Every hit must match an entry
in ``security/fs_allowlist.toml`` keyed by file plus symbol; anything else is an
error, and adding one means editing that file, which shows up in a diff.

It also asserts the thing the whole design rests on: exactly one entry in the
codebase is categorised as unpublished-creation cleanup. If a second external
unlink ever appears, this is what says so.

    python security/check_fs_calls.py
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = Path(__file__).resolve().parent / "fs_allowlist.toml"

# Skipped wholesale: build artefacts, runtime state, this gate's own directory,
# and the tests — the security suite deliberately creates and destroys fixture
# trees, and allowlisting each one would bury the signal.
SKIP_TOP_LEVEL = {"venv", ".venv", "cache", "data", "logs", "tests", "samples",
                  "security"}
SKIP_ANY = {"__pycache__", ".git"}

# Two tiers, because precision is what keeps this gate worth reading.
#
# Tier one: names that mean "change the filesystem" whoever the receiver is.
# None of them has a plausible non-filesystem meaning in this codebase, so any
# appearance is a hit.
ALWAYS_WATCHED = {
    "unlink", "rmtree", "write_text", "write_bytes", "mkdir", "makedirs",
    "touch", "chown", "utime", "system", "symlink", "truncate",
}

# Tier two: names that are filesystem operations on a filesystem object and
# something else entirely on anything else — str.replace, QPainter.save,
# QWidget.move. Flagged only when the receiver is one of the objects below, so
# a real os.replace is caught and a string method is not.
RECEIVER_WATCHED = {
    "remove", "move", "rename", "replace", "chmod", "save", "link",
    "copy", "copy2", "copyfile", "copytree", "open", "write",
}
FILESYSTEM_RECEIVERS = {
    "os", "path", "shutil", "Path", "pathlib", "target", "staged", "source",
    "image", "handle", "file",
}

WATCHED_NAMES = {"open"}
WATCHED_MODULES = {"subprocess"}

WRITE_MODES = ("w", "a", "+", "x")


def load_allowlist():
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 and older
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            print("check_fs_calls needs Python 3.11+ or the 'tomli' package.",
                  file=sys.stderr)
            raise SystemExit(2)

    data = tomllib.loads(ALLOWLIST.read_text(encoding="utf-8"))
    entries = data.get("entry", [])
    allowed = {}
    cleanup = []
    for entry in entries:
        key = (entry["file"], entry["symbol"])
        allowed[key] = entry
        if entry.get("category") == "cleanup":
            cleanup.append(key)
    return allowed, cleanup


def python_files():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in SKIP_TOP_LEVEL:
            continue
        if any(part in SKIP_ANY for part in rel.parts):
            continue
        yield rel, path


def _receiver_name(base):
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return ""


def _call_symbol(node):
    """The name this call should be looked up under, or None if it is harmless."""
    func = node.func

    if isinstance(func, ast.Attribute):
        name = func.attr
        receiver = _receiver_name(func.value)

        if name in ALWAYS_WATCHED:
            return f"{receiver}.{name}" if receiver else name

        if name in RECEIVER_WATCHED:
            if receiver in FILESYSTEM_RECEIVERS or receiver.endswith(("_path", "_dir")):
                return f"{receiver}.{name}"
            return None

        return None

    if isinstance(func, ast.Name):
        if func.id in WATCHED_NAMES:
            return func.id
        return None

    return None


def _open_is_write(node):
    """True when an ``open()`` call asks for a mode that can change a file."""
    if node.args and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        mode = str(node.args[1].value)
        return any(flag in mode for flag in WRITE_MODES)
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            mode = str(keyword.value.value)
            return any(flag in mode for flag in WRITE_MODES)
    return False


def scan(rel, path):
    """Every watched call in one file, as (symbol, line) pairs."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(rel))
    hits = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue

        if isinstance(node, ast.Call):
            symbol = _call_symbol(node)
            if symbol is None:
                continue
            if symbol == "open" and not _open_is_write(node):
                continue
            hits.append((symbol, node.lineno))

            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
                    if keyword.value.value is True:
                        hits.append(("shell=True", node.lineno))

        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in WATCHED_MODULES:
                hits.append(("subprocess", node.lineno))

    return hits


def main():
    allowed, cleanup = load_allowlist()

    failures = []
    seen = set()

    for rel, path in python_files():
        for symbol, line in scan(rel, path):
            if symbol == "shell=True":
                failures.append(f"{rel}:{line}  shell=True is never permitted.")
                continue

            key = (str(rel).replace("\\", "/"), symbol)
            bare = (key[0], symbol.split(".")[-1])
            if key in allowed:
                seen.add(key)
                continue
            if bare in allowed:
                seen.add(bare)
                continue
            failures.append(
                f"{rel}:{line}  '{symbol}' is not in security/fs_allowlist.toml. "
                "Add an entry with a justification, or route it through fs_boundary."
            )

    if len(cleanup) != 1:
        failures.append(
            f"Expected exactly one 'cleanup' entry in the allowlist, found {len(cleanup)}. "
            "There must be exactly one place in this codebase that can unlink an "
            "external file, and it is the gateway's own failure path."
        )

    stale = sorted(set(allowed) - seen)
    for key in stale:
        print(f"note: allowlist entry {key[0]}:{key[1]} no longer matches any call.")

    if failures:
        print("\nFilesystem review gate FAILED:\n", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print("", file=sys.stderr)
        return 1

    print("Filesystem review gate passed: every mutating call is accounted for.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
