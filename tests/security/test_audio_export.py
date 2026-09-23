"""Saving an A-B range as audio only: the MP3 command, its name, and a real encode.

The command is asserted by shape, like every other ffmpeg invocation here. The
encode tests run the bundled ffmpeg on generated media when it is installed, so
"always stereo, never below 128 kbps" is checked on actual output rather than
taken on trust from the arguments.
"""

import re
import subprocess

import pytest

from videotrim import ffmpeg_tools
from videotrim.naming import audio_name


def _value(command, flag):
    return command[command.index(flag) + 1]


def test_the_audio_command_is_a_stereo_192k_mp3_of_the_first_audio_track(tmp_path):
    command = ffmpeg_tools.audio_command("ffmpeg", tmp_path / "in.mp4",
                                         tmp_path / "out.mp3", 2000, 7000)
    assert _value(command, "-c:a") == "libmp3lame"
    assert _value(command, "-b:a") == "192k"
    assert _value(command, "-ac") == "2"
    assert _value(command, "-ar") == "48000"
    assert _value(command, "-map") == "0:a:0"
    assert "-vn" in command
    # A constant bitrate: VBR (-q:a) can fall far below 128 kbps on quiet audio.
    assert "-q:a" not in command and "-vbr" not in command
    assert int(_value(command, "-b:a").rstrip("k")) >= 128
    # Cut exactly like a clip: seek to A, then take B - A.
    assert _value(command, "-ss") == "2.000"
    assert _value(command, "-t") == "5.000"
    assert command[-1] == str(tmp_path / "out.mp3")


def test_audio_is_named_like_a_clip_but_as_mp3():
    assert audio_name("/videos/Holiday.mov", 83400, 105900) == \
        "Holiday_audio_01m23.4s_to_01m45.9s.mp3"
    assert audio_name(None, 0, 1000).endswith(".mp3")


# --- a real encode ---------------------------------------------------------------
@pytest.fixture(scope="module")
def ffmpeg():
    imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - depends on the venv
        pytest.skip("no ffmpeg binary available")


def _make(ffmpeg, target, audio):
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=6"]
    if audio:
        command += ["-f", "lavfi", "-i", audio, "-shortest"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "aac"]
    subprocess.run(command + [str(target)], check=True, timeout=120)
    return target


def _describe(ffmpeg, path):
    text = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, timeout=60).stderr
    stream = re.search(r"Audio: mp3.*?(\d+) Hz, (\w+).*?(\d+) kb/s", text)
    duration = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    seconds = int(duration.group(1)) * 3600 + int(duration.group(2)) * 60 + float(duration.group(3))
    return int(stream.group(1)), stream.group(2), int(stream.group(3)), seconds


@pytest.mark.parametrize("source_audio", [
    "sine=frequency=440:sample_rate=22050:duration=6",   # mono, low rate
    "sine=frequency=440:sample_rate=48000:duration=6",   # mono, video-typical rate
    "aevalsrc=0:channel_layout=5.1:sample_rate=96000:duration=6",  # silent 5.1 at 96 kHz
])
def test_every_source_becomes_a_stereo_mp3_of_at_least_128k(ffmpeg, tmp_path, source_audio):
    source = _make(ffmpeg, tmp_path / "in.mp4", source_audio)
    target = tmp_path / "out.mp3"
    command = ffmpeg_tools.audio_command(ffmpeg, source, target, 1000, 4000, progress=False)
    subprocess.run(command, check=True, timeout=120)

    rate, layout, kbps, seconds = _describe(ffmpeg, target)
    assert layout == "stereo"
    assert rate == 48000
    assert kbps >= 128
    assert abs(seconds - 3.0) < 0.1


def test_has_audio_tells_a_silent_video_apart(ffmpeg, tmp_path, monkeypatch):
    # The probe goes through the containment-checked runner; the test's ffmpeg
    # lives in the test venv, outside any install root, so let it through here.
    monkeypatch.setattr(ffmpeg_tools, "is_internal", lambda path: True)
    with_sound = _make(ffmpeg, tmp_path / "sound.mp4",
                       "sine=frequency=440:sample_rate=48000:duration=6")
    without = _make(ffmpeg, tmp_path / "silent.mp4", None)
    assert ffmpeg_tools.has_audio(ffmpeg, with_sound)
    assert not ffmpeg_tools.has_audio(ffmpeg, without)
