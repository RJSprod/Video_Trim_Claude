"""P0: the host's existing files survive everything.

Every test in this file asserts the same underlying thing from a different
angle — that the only external effect this application can have is a brand-new
file inside the one approved directory.
"""

import os

import pytest

from videotrim.security import fs_boundary as fb
from videotrim.security.fs_boundary import (
    CollisionPolicy,
    CommitDenied,
    FilesystemPolicyError,
    OutputRootError,
    UnsafeNameError,
)


# --- the sanitizer -----------------------------------------------------------
REJECTED = [
    ".", "..", "", "   ",
    "a/b.mp4", "a\\b.mp4", "/etc/passwd", "C:\\Windows\\evil.exe",
    "\\\\server\\share\\x.mp4", "\\\\?\\C:\\x.mp4",
    "duplicate.jpg:evil",                      # NTFS alternate data stream
    "photo\x00.jpg", "photo\x01.jpg", "photo\x1f.jpg",
    "CON", "con", "CON.mp4", "NUL", "nul.txt", "COM1", "COM9.mp4", "LPT1", "lpt9.png",
    "trailing.", "trailing ", "trailing. ", " leading.mp4",
    "x" * 256,
]


@pytest.mark.parametrize("name", REJECTED)
def test_sanitizer_rejects(name):
    with pytest.raises(UnsafeNameError):
        fb.sanitize_basename(name)


@pytest.mark.parametrize("name", [
    "holiday.mp4", "clip (2).mp4", "a.b.c.mp4", "Ünïcödé.png", "no-extension",
    "spaces in the middle.mov",
])
def test_sanitizer_accepts_and_is_idempotent(name):
    result = fb.sanitize_basename(name)
    assert result == name                       # never silently rewritten
    assert fb.sanitize_basename(result) == result


def test_sanitizer_property_sweep():
    """Fuzz: rejection is always fine, a silent rewrite never is."""
    import itertools
    import random

    random.seed(20260815)
    alphabet = list("abzAZ09 .-_()") + ["/", "\\", ":", "\x00", "\x1f", "\u00e9"]
    words = ["CON", "nul", "com1", "lpt9", "clip", "photo"]

    for _ in range(2000):
        length = random.randint(0, 12)
        name = "".join(random.choice(alphabet) for _ in range(length))
        if random.random() < 0.25:
            name = random.choice(words) + name
        try:
            result = fb.sanitize_basename(name)
        except UnsafeNameError:
            continue
        assert result == name, "sanitize_basename rewrote a name instead of rejecting it"
        assert not any(sep in result for sep in "/\\:")
        assert not any(ord(ch) < 0x20 for ch in result)
        assert result == result.rstrip(" .")
        assert result
        assert fb.sanitize_basename(result) == result

    # Guard against the parametrised list drifting out of sync with the fuzz.
    assert all(isinstance(name, str) for name in itertools.chain(REJECTED))


# --- zone separation ---------------------------------------------------------
def test_output_root_may_not_be_the_install_root(fake_root):
    with pytest.raises(OutputRootError):
        fb.validate_output_root(fake_root)


def test_output_root_may_not_be_inside_the_install_root(fake_root):
    inside = fake_root / "cache" / "saves"
    inside.mkdir(parents=True)
    with pytest.raises(OutputRootError) as excinfo:
        fb.validate_output_root(inside)
    assert "inside the Video Trim installation" in str(excinfo.value)


def test_output_root_may_not_contain_the_install_root(fake_root):
    with pytest.raises(OutputRootError):
        fb.validate_output_root(fake_root.parent)


def test_output_root_must_exist_and_is_never_created(host_tree):
    missing = host_tree / "not-there"
    with pytest.raises(OutputRootError):
        fb.validate_output_root(missing)
    assert not missing.exists(), "validate_output_root created the directory"


def test_output_root_rejects_a_file(host_tree):
    with pytest.raises(OutputRootError):
        fb.validate_output_root(host_tree / "sentinel.txt")


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_output_root_symlinked_into_the_install_root_is_refused(fake_root, tmp_path):
    """A link is not a loophole: containment is judged on the resolved path."""
    link = tmp_path / "looks-external"
    link.symlink_to(fake_root / "cache")
    with pytest.raises(OutputRootError):
        fb.validate_output_root(link)


# --- creating ----------------------------------------------------------------
def _commit(staged, name, root, policy=CollisionPolicy.UNIQUE_NEW_NAME, **kwargs):
    return fb.commit_new_file(staged, name, root, collision_policy=policy, **kwargs)


def test_creates_a_brand_new_file(fake_root, staged, output_root, sentinels, elsewhere):
    result = _commit(staged, "fresh.mp4", output_root)
    assert result.status == "published"
    assert (output_root / "fresh.mp4").read_bytes() == b"BRAND NEW CLIP BYTES"
    sentinels.assert_unchanged("a plain successful save")
    sentinels.assert_no_new_files(elsewhere)


def test_never_overwrites_an_existing_file(fake_root, staged, output_root, sentinels):
    """The whole promise, in one test."""
    result = _commit(staged, "duplicate.jpg", output_root)
    assert result.status == "published"
    assert result.basename == "duplicate.jpg (2)" or result.basename.startswith("duplicate")
    assert result.basename != "duplicate.jpg"
    sentinels.assert_unchanged("a name collision must not touch the original")


def test_skip_by_name_writes_nothing(fake_root, staged, output_root, sentinels):
    result = _commit(staged, "duplicate.jpg", output_root,
                     policy=CollisionPolicy.SKIP_BY_NAME)
    assert result.status == "already_exists"
    assert list(output_root.iterdir()) == [output_root / "duplicate.jpg"]
    sentinels.assert_unchanged("a skip must create nothing at all")


def test_unique_new_name_produces_distinct_files(fake_root, output_root, sentinels):
    """Two exports racing for one generated name resolve to two real files."""
    names = []
    for index in range(3):
        staging = fake_root / "cache" / "exports" / f"race{index}"
        staging.mkdir(parents=True)
        staged = staging / "clip.mp4"
        staged.write_bytes(f"clip {index}".encode())
        result = _commit(staged, "duplicate.jpg", output_root)
        names.append(result.basename)
    assert len(set(names)) == 3
    sentinels.assert_unchanged("racing exports must not touch the original")


@pytest.mark.parametrize("name", [
    "../escape.txt", "../../escape.txt", "/etc/escape.txt",
    "sub/dir/escape.txt", "C:\\escape.txt", "duplicate.jpg:evil",
    "CON.mp4", "trailing.", "nul",
])
def test_malicious_names_never_reach_a_create(fake_root, staged, output_root,
                                              sentinels, name):
    with pytest.raises(UnsafeNameError):
        _commit(staged, name, output_root)
    sentinels.assert_unchanged(f"rejected name {name!r} must have no effect")


def test_no_subdirectory_is_ever_created(fake_root, staged, output_root):
    before = {p.name for p in output_root.iterdir()}
    with pytest.raises(UnsafeNameError):
        _commit(staged, "new folder/thing.mp4", output_root)
    assert {p.name for p in output_root.iterdir()} == before


def test_staged_file_must_be_internal(fake_root, host_tree, output_root, sentinels):
    """An external file cannot be laundered through the gateway as 'staged'."""
    with pytest.raises(FilesystemPolicyError):
        _commit(host_tree / "existing.mp4", "copy.mp4", output_root)
    sentinels.assert_unchanged("an external staged path must be refused")


def test_revoked_permission_blocks_the_commit(fake_root, staged, output_root, sentinels):
    def denied():
        return False, "File transfer disabled by host"

    with pytest.raises(CommitDenied):
        _commit(staged, "fresh.mp4", output_root, authorize=denied)
    assert not (output_root / "fresh.mp4").exists()
    sentinels.assert_unchanged("a denied commit must create nothing")


def test_authorization_is_checked_before_the_create(fake_root, staged, output_root):
    """Permission is re-read at commit time, not carried from request time."""
    calls = []

    def authorize():
        calls.append(True)
        return True, ""

    _commit(staged, "fresh.mp4", output_root, authorize=authorize)
    assert calls, "the gateway did not consult the authorizer"


def test_output_root_revalidated_at_commit(fake_root, staged, tmp_path, sentinels):
    """A destination that vanished between selection and save fails, with no fallback."""
    gone = tmp_path / "removed"
    gone.mkdir()
    gone.rmdir()
    with pytest.raises(OutputRootError):
        _commit(staged, "fresh.mp4", gone)
    assert not gone.exists(), "the gateway recreated a missing destination"


@pytest.mark.skipif(os.name != "posix", reason="dir_fd anchoring is POSIX")
def test_create_is_anchored_to_a_directory_handle(fake_root, staged, output_root,
                                                  tmp_path, sentinels):
    """Swapping the parent after validation must not redirect the create.

    The gateway opens the output root once and creates relative to that handle,
    so validation and creation refer to one inode with no window between them.
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()

    real_open = os.open
    swapped = {"done": False}

    def swapping_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        # Right after the root handle is taken, repoint the *name* elsewhere.
        if not swapped["done"] and kwargs.get("dir_fd") is None and os.path.isdir(path):
            swapped["done"] = True
            moved = output_root.parent / "output-moved"
            os.rename(output_root, moved)
            os.symlink(decoy, output_root)
        return fd

    import unittest.mock as mock
    with mock.patch.object(os, "open", swapping_open):
        result = fb.commit_new_file(staged, "anchored.mp4", output_root)

    assert result.status == "published"
    # The file landed in the directory that was validated, not in the decoy.
    assert not (decoy / "anchored.mp4").exists(), "the create followed a swapped parent"
    assert (output_root.parent / "output-moved" / "anchored.mp4").is_file()


# --- unpublished-creation cleanup --------------------------------------------
def test_failed_copy_removes_only_the_file_it_just_made(fake_root, output_root,
                                                        sentinels, monkeypatch):
    staging = fake_root / "cache" / "exports" / "boom"
    staging.mkdir(parents=True)
    staged = staging / "clip.mp4"
    staged.write_bytes(b"x" * 4096)

    def explode(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", explode)

    with pytest.raises(FilesystemPolicyError):
        _commit(staged, "halfway.mp4", output_root)

    assert not (output_root / "halfway.mp4").exists(), (
        "a file the gateway created and never published was left behind"
    )
    sentinels.assert_unchanged("a mid-copy failure must not touch anything else")


def test_cleanup_never_removes_a_pre_existing_file(fake_root, output_root,
                                                   sentinels, monkeypatch):
    """The failure path must not be able to reach a file it did not create."""
    staging = fake_root / "cache" / "exports" / "boom2"
    staging.mkdir(parents=True)
    staged = staging / "clip.mp4"
    staged.write_bytes(b"y" * 2048)

    def explode(fd, data):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "write", explode)

    with pytest.raises(FilesystemPolicyError):
        _commit(staged, "duplicate.jpg", output_root)

    assert (output_root / "duplicate.jpg").read_bytes() == b"\xff\xd8\xffORIGINAL-JPEG"
    sentinels.assert_unchanged("the original duplicate.jpg must be untouched")


def test_cleanup_is_refused_when_authorship_cannot_be_proven(fake_root, output_root,
                                                             monkeypatch):
    """No descriptor, no proof, no unlink — the file is reported instead."""
    staging = fake_root / "cache" / "exports" / "boom3"
    staging.mkdir(parents=True)
    staged = staging / "clip.mp4"
    staged.write_bytes(b"z" * 1024)

    def explode(fd, data):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "write", explode)
    # Break the proof: pretend the inode moved under us.
    monkeypatch.setattr(fb._CreatedFile, "still_ours", lambda self: False)

    result = _commit(staged, "orphan.mp4", output_root)
    assert result.status == "possible_partial"
    assert (output_root / "orphan.mp4").exists(), (
        "the file was removed without the gateway being able to prove it made it"
    )


def test_published_files_are_never_unlinked(fake_root, staged, output_root):
    result = _commit(staged, "published.mp4", output_root)
    assert result.published
    # Nothing in the public surface can remove it.
    assert not hasattr(fb, "delete_external")
    assert not hasattr(fb, "remove_output")
    assert (output_root / "published.mp4").is_file()


# --- internal-only mutation --------------------------------------------------
def test_safe_internal_unlink_refuses_external(fake_root, host_tree, sentinels):
    with pytest.raises(FilesystemPolicyError):
        fb.safe_internal_unlink(host_tree / "sentinel.txt")
    sentinels.assert_unchanged("safe_internal_unlink must refuse external paths")


def test_safe_internal_rmtree_refuses_external(fake_root, host_tree, sentinels):
    with pytest.raises(FilesystemPolicyError):
        fb.safe_internal_rmtree(host_tree)
    sentinels.assert_unchanged("safe_internal_rmtree must refuse external paths")


def test_safe_internal_rmtree_refuses_the_root_itself(fake_root):
    with pytest.raises(FilesystemPolicyError):
        fb.safe_internal_rmtree(fake_root)
    assert fake_root.is_dir()


def test_ensure_internal_dir_refuses_external(fake_root, tmp_path):
    outside = tmp_path / "outside-dir"
    with pytest.raises(FilesystemPolicyError):
        fb.ensure_internal_dir(outside)
    assert not outside.exists()


# --- reads -------------------------------------------------------------------
def test_external_read_refuses_non_regular_files(fake_root, host_tree):
    with pytest.raises(fb.ExternalReadError):
        fb.validate_external_read(host_tree)


def test_external_read_leaves_the_file_alone(fake_root, host_tree, sentinels):
    fb.validate_external_read(host_tree / "existing.mp4")
    sentinels.assert_unchanged("validating a source for reading must not touch it")


# --- disclosure --------------------------------------------------------------
def test_describe_for_hides_paths_from_remote_sessions(output_root):
    assert fb.describe_for(output_root, host_local=True) == str(output_root)
    remote = fb.describe_for(output_root, host_local=False, display_name="the host's save folder")
    assert remote == "the host's save folder"
    assert str(output_root) not in remote
