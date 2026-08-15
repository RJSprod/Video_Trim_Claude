"""The fixture tree every filesystem test asserts against.

A fake install root, a host tree outside it holding pre-existing files, and a
second external tree that is *not* the output root — that one exists purely to
prove nothing ever writes there.

Before each scenario the sentinels' content hash, size, mtime and (on POSIX)
st_dev/st_ino are recorded. After it, they must be identical. "The test passed"
is not the assertion; "the user's files are exactly as they were" is.
"""

import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def snapshot(root):
    """Content hash plus metadata for every file under ``root``."""
    state = {}
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        info = path.stat()
        state[str(path.relative_to(root))] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "dev": info.st_dev if os.name == "posix" else None,
            "ino": info.st_ino if os.name == "posix" else None,
            "streams": sorted(p.name for p in path.parent.glob(path.name + ":*")),
        }
    return state


class Sentinels:
    """Holds a before-snapshot and asserts nothing moved."""

    def __init__(self, *roots):
        self.roots = [Path(root) for root in roots]
        self.before = {str(root): snapshot(root) for root in self.roots}

    def assert_unchanged(self, message=""):
        """Every file that existed before is byte-for-byte and stat-for-stat the same.

        Deliberately says nothing about *new* files: creating one in the approved
        output root is the single external effect the app is allowed to have.
        Whether a new file appeared where it should not is a separate assertion —
        assert_no_new_files — so a passing test cannot mean two things at once.
        """
        for root in self.roots:
            after = snapshot(root)
            before = self.before[str(root)]
            for name, state in before.items():
                assert name in after, (
                    f"{root / name} was deleted or moved. {message}"
                )
                assert after[name] == state, (
                    f"{root / name} was modified. {message}\n"
                    f"before={state}\nafter={after[name]}"
                )

    def assert_no_new_files(self, root):
        after = snapshot(root)
        new = set(after) - set(self.before[str(root)])
        assert not new, (
            f"Files were created under {root}, which nothing is allowed to write to: "
            f"{sorted(new)}"
        )

    def created_under(self, root):
        """Names that appeared since the snapshot, for asserting on the output root."""
        after = snapshot(root)
        return sorted(set(after) - set(self.before[str(root)]))


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """A stand-in install root, so tests never touch the real one."""
    root = tmp_path / "install"
    (root / "venv").mkdir(parents=True)
    (root / "cache").mkdir()
    (root / "data").mkdir()
    (root / "logs").mkdir()

    from videotrim.security import fs_boundary

    monkeypatch.setattr(fs_boundary, "INSTALL_ROOT", root.resolve())
    monkeypatch.setattr(fs_boundary, "DATA_DIR", root / "data")
    monkeypatch.setattr(fs_boundary, "CACHE_DIR", root / "cache")
    monkeypatch.setattr(fs_boundary, "LOGS_DIR", root / "logs")
    monkeypatch.setattr(fs_boundary, "VENV_DIR", root / "venv")
    monkeypatch.setattr(
        fs_boundary,
        "_PROTECTED_ZONES",
        (root / "venv", root / "data", root / "cache", root / "logs"),
    )
    return root.resolve()


@pytest.fixture
def host_tree(tmp_path):
    """The user's own files, outside the install root, which must never change."""
    external = tmp_path / "external_host"
    output = external / "output"
    output.mkdir(parents=True)

    (external / "sentinel.txt").write_text("do not touch me", encoding="utf-8")
    (external / "existing.mp4").write_bytes(b"\x00original video bytes\x00")
    (output / "duplicate.jpg").write_bytes(b"\xff\xd8\xffORIGINAL-JPEG")
    return external


@pytest.fixture
def elsewhere(tmp_path):
    """A second external tree that is not the output root. Nothing may write here."""
    other = tmp_path / "not_the_output_root"
    other.mkdir()
    (other / "bystander.txt").write_text("uninvolved", encoding="utf-8")
    return other


@pytest.fixture
def output_root(host_tree):
    return host_tree / "output"


@pytest.fixture
def sentinels(host_tree, elsewhere):
    return Sentinels(host_tree, elsewhere)


@pytest.fixture
def staged(fake_root):
    """A completed internal file, ready to be published."""
    staging = fake_root / "cache" / "exports" / "job1"
    staging.mkdir(parents=True)
    target = staging / "clip.mp4"
    target.write_bytes(b"BRAND NEW CLIP BYTES")
    return target
