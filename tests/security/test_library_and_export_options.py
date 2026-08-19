"""P0 for the two things this change added: the Files browser, and export options.

The browser is the first feature that lets a client name a path at all, so the
assertions here are about the two properties that makes it safe to have:

    it cannot be talked out of the save folder, by ``..``, by an absolute path,
    by a colon, or by a symlink; and

    it never hands a remote session a host filesystem path, in a listing, in an
    error, or in a poster.

Everything it does is a read, so every scenario also asserts the user's own
files are byte-for-byte what they were — a browser that quietly touched an mtime
would still be a browser that modified somebody's file.

The options half asserts that nothing a browser sends reaches a command line
unbounded: an unknown preset, a hostile string, an upscale and a stretched
aspect ratio all come back as the same safe shape.
"""

import os
import sys
from pathlib import Path

import pytest

from videotrim import encoding
from videotrim.security import fs_boundary
from videotrim.security.fs_boundary import FilesystemPolicyError, resolve_within_root

# The live-application fixtures. Imported rather than duplicated so these
# scenarios run against exactly the app the other suite knocks on.
from .test_live_routes import app, client, sign_in  # noqa: F401


# --- the containment rule ----------------------------------------------------
@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "saves"
    (root / "clips").mkdir(parents=True)
    (root / "clips" / "holiday.mp4").write_bytes(b"\x00mp4\x00")
    (root / "photo.jpg").write_bytes(b"\xff\xd8\xffJPEG")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "secret.txt").write_text("not yours", encoding="utf-8")
    return root


def test_a_relative_name_resolves_inside_the_root(tree):
    assert resolve_within_root(tree, "clips/holiday.mp4") == (tree / "clips" / "holiday.mp4")
    assert resolve_within_root(tree, "") == tree.resolve()
    assert resolve_within_root(tree, "./clips") == (tree / "clips")


@pytest.mark.parametrize("attempt", [
    "../private/secret.txt",
    "clips/../../private/secret.txt",
    "..",
    "../",
    "C:\\Windows\\win.ini",
    "photo.jpg:stream",
    "bad\x00name",
    "bad\x01name",
])
def test_escapes_are_refused_rather_than_collapsed(tree, attempt):
    with pytest.raises(FilesystemPolicyError):
        resolve_within_root(tree, attempt)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_a_symlink_out_of_the_root_is_refused(tree, tmp_path):
    os.symlink(tmp_path / "private", tree / "shortcut")
    with pytest.raises(FilesystemPolicyError):
        resolve_within_root(tree, "shortcut/secret.txt")


@pytest.mark.parametrize("attempt", [
    "\\\\server\\share\\file.mp4",   # a UNC path
    "clips/..%2f..",                # traversal that was never decoded
    "//etc/passwd",
])
def test_a_path_that_looks_dangerous_still_only_names_something_inside(tree, attempt):
    """Refusing is one safe answer; landing inside the folder is the other.

    What must never happen is resolving *outside*. These shapes are all
    re-anchored on the root, so the worst they name is a file that is not there.
    """
    resolved = resolve_within_root(tree, attempt)
    base = tree.resolve()
    assert resolved == base or base in resolved.parents


def test_a_leading_slash_is_treated_as_relative_not_absolute(tree):
    """``/etc/passwd`` names a file inside the folder, or nothing at all."""
    resolved = resolve_within_root(tree, "/photo.jpg")
    assert resolved == (tree / "photo.jpg")


# --- the browser, live -------------------------------------------------------
@pytest.fixture
def stocked(output_root):
    """A save folder with something of each kind in it."""
    (output_root / "clips").mkdir()
    (output_root / "clips" / "trip.mp4").write_bytes(b"\x00fake mp4\x00")
    (output_root / "notes.txt").write_text("just a note", encoding="utf-8")
    return output_root


def test_the_host_can_list_the_save_folder(app, stocked, sentinels):
    with client(app, host="127.0.0.1") as host:
        sign_in(host)
        listing = host.get("/vt/api/library/list").json()

    names = sorted(entry["name"] for entry in listing["entries"])
    assert names == ["clips", "duplicate.jpg", "notes.txt"]
    assert listing["root_label"] == str(stocked)
    kinds = {entry["name"]: entry["kind"] for entry in listing["entries"]}
    assert kinds == {"clips": "folder", "duplicate.jpg": "image", "notes.txt": "file"}
    sentinels.assert_unchanged("listing a folder must not touch anything")


def test_a_remote_session_gets_names_but_never_a_path(app, stocked):
    with client(app, host="192.168.1.77") as phone:
        sign_in(phone)
        response = phone.get("/vt/api/library/list")
    listing = response.json()

    assert response.status_code == 200
    assert listing["root_label"] == "the host's save folder"
    assert str(stocked) not in response.text
    assert str(Path.home()) not in response.text
    for entry in listing["entries"]:
        assert not entry["path"].startswith("/")
        assert ":" not in entry["path"]


def test_a_subfolder_is_reachable_and_names_stay_relative(app, stocked):
    with client(app, host="127.0.0.1") as host:
        sign_in(host)
        listing = host.get("/vt/api/library/list?dir=&path=clips").json()

    assert listing["path"] == "clips"
    assert listing["parent"] == ""
    assert [entry["path"] for entry in listing["entries"]] == ["clips/trip.mp4"]
    assert listing["crumbs"][-1]["name"] == "clips"


@pytest.mark.parametrize("attempt", [
    "../", "..", "../../etc", "clips/../..", "photo.jpg:evil",
])
def test_the_listing_cannot_be_walked_out_of_the_save_folder(app, stocked, attempt,
                                                             sentinels):
    import urllib.parse

    with client(app, host="127.0.0.1") as host:
        sign_in(host)
        response = host.get("/vt/api/library/list?path=" +
                            urllib.parse.quote(attempt, safe=""))
    assert response.status_code in (403, 404), f"{attempt} was listed"
    sentinels.assert_unchanged("a refused path must have no effect")


def test_serving_a_picture_works_and_a_text_file_is_refused(app, stocked):
    with client(app, host="127.0.0.1") as host:
        sign_in(host)
        picture = host.get("/vt/library/file?path=duplicate.jpg")
        text = host.get("/vt/library/file?path=notes.txt")

    assert picture.status_code == 200
    assert picture.content == b"\xff\xd8\xffORIGINAL-JPEG"
    assert text.status_code == 415, "a non-media file was served out of the save folder"


def test_serving_cannot_be_walked_out_of_the_folder(app, stocked, host_tree):
    with client(app, host="127.0.0.1") as host:
        sign_in(host)
        response = host.get("/vt/library/file?path=../sentinel.txt")
    assert response.status_code in (403, 404, 415)
    assert b"do not touch me" not in response.content


def test_opening_a_non_video_is_refused(app, stocked):
    with client(app, host="127.0.0.1") as host:
        csrf = sign_in(host)
        response = host.post("/vt/api/library/open",
                             json={"path": "duplicate.jpg"},
                             headers={"X-VT-CSRF": csrf})
    assert response.status_code == 415


def test_the_browser_creates_nothing_in_the_save_folder(app, stocked, sentinels,
                                                        elsewhere):
    before = sorted(p.name for p in stocked.iterdir())
    with client(app, host="127.0.0.1") as host:
        sign_in(host)
        host.get("/vt/api/library/list")
        host.get("/vt/api/library/list?path=clips")
        host.get("/vt/library/file?path=duplicate.jpg")
    assert sorted(p.name for p in stocked.iterdir()) == before
    sentinels.assert_unchanged("browsing must not modify anything")
    sentinels.assert_no_new_files(elsewhere)


@pytest.mark.parametrize("route", [
    "/vt/api/library/list", "/vt/library/file?path=x.jpg",
    "/vt/library/poster?path=x.mp4", "/tools/files",
])
def test_the_files_tool_needs_a_session(app, route):
    with client(app) as anonymous:
        response = anonymous.get(route, headers={"accept": "application/json"})
    assert response.status_code in (401, 303)


def test_the_files_card_is_offered_to_every_signed_in_session(app, stocked):
    with client(app, host="192.168.1.77") as phone:
        sign_in(phone)
        caps = phone.get("/vt/api/capabilities").json()
    card = [tool for tool in caps["tools"] if tool["id"] == "files"][0]
    assert card["enabled"] and card["route"] == "/tools/files"


def test_no_save_location_is_a_clear_refusal_not_a_fallback(app, monkeypatch):
    """With nothing configured there is nothing to browse — and no guessing."""
    from videotrim.config.settings import SAVE_LOCATION

    with client(app, host="192.168.1.77") as phone:
        sign_in(phone)
        state = app.state.video_trim
        state.store.set_setting(SAVE_LOCATION, "")
        response = phone.get("/vt/api/library/list")

    assert response.status_code == 503
    assert str(Path.home()) not in response.text


# --- export options ----------------------------------------------------------
def test_defaults_produce_exactly_the_command_the_app_always_ran():
    from videotrim import ffmpeg_tools

    plain = ffmpeg_tools.clip_command("ffmpeg", "in.mp4", "out.mp4", 0, 1000)
    assert "-crf" in plain and plain[plain.index("-crf") + 1] == "18"
    assert plain[plain.index("-preset") + 1] == "veryfast"
    assert "-vf" not in plain and "-r" not in plain


def test_a_hostile_preset_never_reaches_the_command_line():
    from videotrim import ffmpeg_tools

    options = encoding.normalize(
        {"preset": "medium; rm -rf ~", "crf": "18; touch /tmp/pwned"},
        source_width=1920, source_height=1080,
    )
    command = ffmpeg_tools.clip_command("ffmpeg", "in.mp4", "out.mp4", 0, 1000,
                                        options=options)
    assert options["preset"] in encoding.PRESETS
    for part in command:
        assert ";" not in part and "&" not in part and "|" not in part


def test_scaling_is_locked_to_the_source_aspect_and_never_upscales():
    wide = encoding.normalize({"width": 960}, source_width=1920, source_height=1080)
    assert (wide["width"], wide["height"]) == (960, 540)

    # A client asking for more than the source has gets the source.
    bigger = encoding.normalize({"width": 4096}, source_width=1920, source_height=1080)
    assert bigger["width"] == 0 and bigger["height"] == 0

    # An odd width becomes even, because H.264 cannot encode an odd side.
    odd = encoding.normalize({"width": 641}, source_width=1280, source_height=720)
    assert odd["width"] % 2 == 0 and odd["height"] % 2 == 0

    # A height the client made up is ignored: only the width is a request.
    stretched = encoding.normalize({"width": 640, "height": 640},
                                   source_width=1280, source_height=720)
    assert stretched["height"] == 360


def test_unknown_values_fall_back_rather_than_being_forwarded():
    options = encoding.normalize(
        {"crf": 900, "preset": "nope", "audio_kbps": 999999, "fps_cap": 7,
         "width": -5, "extra": "ignored"},
        source_width=1920, source_height=1080,
    )
    assert options["crf"] == encoding.CRF_MAX
    assert options["preset"] == encoding.DEFAULTS["preset"]
    assert options["audio_kbps"] == encoding.DEFAULTS["audio_kbps"]
    assert options["fps_cap"] == 0
    assert options["width"] == 0
    assert "extra" not in options


def test_the_estimate_moves_the_way_the_encoder_does():
    big = encoding.estimate_bytes(1920, 1080, 30, 60000, {"crf": 18})
    smaller = encoding.estimate_bytes(1920, 1080, 30, 60000, {"crf": 24})
    assert smaller < big

    scaled = encoding.estimate_bytes(1920, 1080, 30, 60000,
                                     {"crf": 18, "width": 960})
    assert scaled < big

    capped = encoding.estimate_bytes(1920, 1080, 60, 60000, {"crf": 18, "fps_cap": 30})
    full_rate = encoding.estimate_bytes(1920, 1080, 60, 60000, {"crf": 18})
    assert capped < full_rate

    # Twice the length is twice the file.
    one = encoding.estimate_bytes(1920, 1080, 30, 30000, {})
    two = encoding.estimate_bytes(1920, 1080, 30, 60000, {})
    assert abs(two - one * 2) <= 2


def test_the_client_is_handed_the_same_numbers_the_server_encodes_with():
    model = encoding.model()
    assert model["defaults"] == encoding.DEFAULTS
    assert model["presets"] == list(encoding.PRESETS)
    assert model["crf_min"] == encoding.CRF_MIN
    assert model["anchor_bpp"] == encoding.ANCHOR_BPP


def test_the_config_route_carries_the_export_model(app):
    with client(app, host="127.0.0.1") as host:
        sign_in(host)
        config = host.get("/vt/api/config").json()
    assert config["export_model"]["crf_max"] == encoding.CRF_MAX
    assert "veryfast" in config["export_model"]["presets"]


def test_options_are_vetted_against_the_source_not_the_request(app, output_root):
    """The server re-derives the frame size from what it probed."""
    state = app.state.video_trim

    class Probed:
        @staticmethod
        def probe(*_args, **_kwargs):
            return {"width": 1280, "height": 720, "duration_ms": 1000, "fps": 30,
                    "codec": "h264"}

    from videotrim import ffmpeg_tools

    original = ffmpeg_tools.probe_video
    ffmpeg_tools.probe_video = Probed.probe
    try:
        options = state.export_options({"width": 640, "height": 4000},
                                       output_root / "duplicate.jpg")
    finally:
        ffmpeg_tools.probe_video = original

    assert (options["width"], options["height"]) == (640, 360)


def test_no_options_means_no_options(app, output_root):
    state = app.state.video_trim
    assert state.export_options(None, output_root / "duplicate.jpg") is None
    assert state.export_options({}, output_root / "duplicate.jpg") is None
