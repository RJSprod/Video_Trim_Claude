"""End-to-end: boot the real application and knock on its doors.

The unit tests prove each rule in isolation. This one proves the rules are
actually *wired* — that the middleware really wraps the outer app, that a
Gradio-owned route really 401s, and that a remote client really cannot reach
Settings by typing the URL.
"""

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("gradio")

from videotrim.config.settings import SettingsService
from videotrim.config.store import Store
from videotrim.security.auth import AuthService

USERNAME = "hostuser"
PASSWORD = "a long enough password"


@pytest.fixture
def app(fake_root, output_root, monkeypatch):
    from videotrim.web import server

    # Keep the app's scratch space inside the fake root too.
    monkeypatch.setattr(server, "ROOT", fake_root)
    monkeypatch.setattr(server, "CACHE", fake_root / "cache")
    monkeypatch.setattr(server, "UPLOAD_DIR", fake_root / "cache" / "uploads")
    monkeypatch.setattr(server, "PROXY_DIR", fake_root / "cache" / "proxies")
    monkeypatch.setattr(server, "GRADIO_TEMP", fake_root / "cache" / "gradio")

    store = Store()
    AuthService(store).create_credentials(USERNAME, PASSWORD)
    SettingsService(store).set_save_location(output_root)
    return server.create_app(store=store)


class LiveClient:
    """A synchronous driver for the real ASGI app.

    httpx only speaks ASGI asynchronously, and these tests read far better
    written straight through, so one event loop per client bridges the two. The
    peer address is spoofed at the transport, which is exactly the knob that
    decides host-local versus remote — so "the host" and "a phone" are the same
    application with a different ``client`` tuple.
    """

    def __init__(self, app, host="127.0.0.1"):
        import asyncio

        self._loop = asyncio.new_event_loop()
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(host, 51234)),
            base_url="http://testserver",
            follow_redirects=False,
        )

    @property
    def cookies(self):
        return self._client.cookies

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def get(self, url, **kwargs):
        return self._run(self._client.get(url, **kwargs))

    def post(self, url, **kwargs):
        return self._run(self._client.post(url, **kwargs))

    def close(self):
        self._run(self._client.aclose())
        self._loop.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def client(app, host="127.0.0.1"):
    """A client whose requests appear to come from ``host``."""
    return LiveClient(app, host)


def sign_in(session):
    response = session.post("/api/login",
                            json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return session.cookies.get("vt_csrf")


# --- default deny ------------------------------------------------------------
UNAUTHENTICATED_DENIED = [
    "/", "/settings", "/tools/media-transfer", "/tools/video-trim",
    "/vt/api/capabilities", "/vt/api/config", "/vt/api/browse",
    "/vt/api/settings", "/vt/api/transfer/status", "/vt/static/player.js",
    # Routes Gradio contributes, which this codebase does not author.
    "/gradio_api/file=/etc/passwd", "/gradio_api/file/etc/passwd",
    "/queue/join", "/config", "/info", "/upload",
    "/tools/video-trim/config", "/tools/video-trim/info",
]


@pytest.mark.parametrize("path", UNAUTHENTICATED_DENIED)
def test_nothing_is_reachable_without_a_session(app, path):
    with client(app) as session:
        response = session.get(path)
    assert response.status_code in (401, 303, 404), (
        f"{path} answered {response.status_code} to an unauthenticated client"
    )
    if response.status_code == 303:
        assert response.headers["location"] == "/login"


def test_a_gradio_owned_route_401s_without_a_session(app):
    """The regression test that stops a Gradio upgrade silently reopening things."""
    with client(app) as session:
        response = session.get("/gradio_api/file=/etc/passwd",
                               headers={"accept": "application/json"})
    assert response.status_code == 401, (
        "a Gradio-owned route was reachable without authentication"
    )


@pytest.mark.parametrize("path", [
    "/login/../gradio_api/file=/etc/passwd",
    "/vt/static/login.css/../player.js",
    "/healthz/../vt/api/settings",
])
def test_allowlist_cannot_be_reached_by_substring(app, path):
    with client(app) as session:
        response = session.get(path, headers={"accept": "application/json"})
    assert response.status_code != 200, f"{path} slipped past the allowlist"


def test_only_the_login_surface_is_public(app):
    with client(app) as session:
        assert session.get("/login").status_code == 200
        assert session.get("/healthz").status_code == 200
        assert session.get("/vt/static/login.css").status_code == 200
        assert session.get("/vt/static/login.js").status_code == 200


# --- login -------------------------------------------------------------------
def test_bad_password_is_refused_and_says_nothing_useful(app):
    with client(app) as session:
        response = session.post("/api/login",
                                json={"username": USERNAME, "password": "wrong one"})
        assert response.status_code == 401
        missing = session.post("/api/login",
                               json={"username": "ghost", "password": "wrong one"})
        assert missing.json()["detail"] == response.json()["detail"]


def test_sign_in_then_home(app):
    with client(app) as session:
        sign_in(session)
        response = session.get("/")
        assert response.status_code == 200
        assert "Video Trim" in response.text


def test_logout_revokes_the_session(app):
    with client(app) as session:
        csrf = sign_in(session)
        assert session.get("/vt/api/capabilities").status_code == 200
        assert session.post("/api/logout", headers={"X-VT-CSRF": csrf}).status_code == 200
        assert session.get("/vt/api/capabilities",
                           headers={"accept": "application/json"}).status_code == 401


# --- CSRF --------------------------------------------------------------------
def test_a_mutation_without_the_csrf_header_is_refused(app):
    with client(app) as session:
        sign_in(session)
        response = session.post("/vt/api/transfer/request-access")
    assert response.status_code == 403


def test_a_mutation_with_a_wrong_csrf_header_is_refused(app):
    with client(app) as session:
        sign_in(session)
        response = session.post("/vt/api/transfer/request-access",
                                headers={"X-VT-CSRF": "not-the-token"})
    assert response.status_code == 403


# Every state-changing route the front end calls. Each one is exercised twice:
# once without the CSRF header to prove the guard is live, and once with it to
# prove the guard is not the thing breaking the feature. The second half is the
# half that matters — a shipped client that never sends the header turns every
# one of these into a dead button, which is exactly what happened to
# /vt/api/upload when its XHR was written by hand instead of going through the
# shared sender.
MUTATING_ROUTES = [
    "/vt/api/upload?name=clip.mp4",
    "/vt/api/still?name=clip.mp4&position_ms=0",
    "/vt/api/transfer/upload?name=photo.jpg",
    "/vt/api/transfer/request-access",
    "/vt/api/settings/save-label",
]


@pytest.mark.parametrize("route", MUTATING_ROUTES)
def test_mutating_route_requires_csrf(app, route):
    with client(app) as session:
        sign_in(session)
        response = session.post(route, content=b"x")
    assert response.status_code == 403, f"{route} accepted a request with no CSRF token"


@pytest.mark.parametrize("route", MUTATING_ROUTES)
def test_mutating_route_accepts_the_token_the_client_holds(app, route):
    """The regression guard: the token in the cookie must actually work.

    A 400/413/415/503 here is fine — that is the route judging the body. A 403
    is not: it means a correctly-behaved client is being turned away.
    """
    with client(app) as session:
        csrf = sign_in(session)
        response = session.post(route, headers={"X-VT-CSRF": csrf}, content=b"x")
    assert response.status_code != 403, (
        f"{route} rejected the CSRF token the client was given"
    )


def test_every_front_end_request_carries_the_csrf_header():
    """Static guard over the shipped JS.

    The bug was not a missing rule, it was one request that did not go through
    the code implementing the rule. So this asserts on shape: every
    XMLHttpRequest in the assets sets the header, and every POST helper does
    too. Adding a hand-rolled XHR without it fails here rather than in
    somebody's browser.
    """
    from videotrim.web.server import ASSETS

    for name in ("player.js", "shell.js"):
        source = (ASSETS / name).read_text(encoding="utf-8")
        opens = source.count("new XMLHttpRequest()")
        headers = source.count("X-VT-CSRF")
        assert opens > 0, f"{name} has no XHR; update this test if that changed"
        assert headers >= opens, (
            f"{name} creates {opens} XMLHttpRequest(s) but sets the CSRF header "
            f"only {headers} time(s) — one of them will 403 at runtime"
        )


# --- host admin --------------------------------------------------------------
def test_remote_client_cannot_reach_settings(app):
    with client(app, host="192.168.1.77") as session:
        sign_in(session)
        assert session.get("/settings",
                           headers={"accept": "application/json"}).status_code == 403
        assert session.get("/vt/api/settings").status_code == 403
        assert session.post("/vt/api/settings/save-location",
                            json={"path": "/tmp"},
                            headers={"X-VT-CSRF": session.cookies.get("vt_csrf")}
                            ).status_code == 403


def test_remote_capabilities_have_no_settings_and_no_path(app, output_root):
    with client(app, host="192.168.1.77") as session:
        sign_in(session)
        caps = session.get("/vt/api/capabilities").json()
    assert caps["settings_count"] == 0
    assert "output_path" not in caps
    assert str(output_root) not in session.__class__.__name__  # sanity
    assert caps["output_display_name"] == "the host's save folder"


def test_host_capabilities_include_settings(app, output_root):
    with client(app, host="127.0.0.1") as session:
        sign_in(session)
        caps = session.get("/vt/api/capabilities").json()
    assert caps["is_host_admin"]
    assert caps["settings_count"] > 0
    assert caps["output_path"] == str(output_root)


def test_forwarded_header_cannot_forge_host_status(app):
    with client(app, host="192.168.1.77") as session:
        sign_in(session)
        response = session.get("/vt/api/settings",
                               headers={"X-Forwarded-For": "127.0.0.1"})
    assert response.status_code == 403


# --- write policy ------------------------------------------------------------
def test_new_remote_address_cannot_transfer(app, sentinels, host_tree):
    with client(app, host="192.168.1.77") as session:
        csrf = sign_in(session)
        status = session.get("/vt/api/transfer/status").json()
        assert not status["allowed"]
        assert status["reason"] == "File transfer disabled by host"
        assert status["can_request_access"]

        response = session.post(
            "/vt/api/transfer/upload?name=photo.jpg",
            headers={"X-VT-CSRF": csrf, "Content-Type": "application/octet-stream"},
            content=b"\xff\xd8\xffJPEGDATA",
        )
    assert response.status_code == 403
    sentinels.assert_unchanged("a write-denied client must not create anything")


def test_requesting_access_changes_nothing_about_permission(app):
    with client(app, host="192.168.1.77") as session:
        csrf = sign_in(session)
        response = session.post("/vt/api/transfer/request-access",
                                headers={"X-VT-CSRF": csrf})
        assert response.status_code == 200
        assert "Access requested" in response.json()["message"]

        status = session.get("/vt/api/transfer/status").json()
        assert not status["allowed"], "requesting access granted it"
        assert status["access_requested"]


def test_host_allows_an_address_and_the_transfer_then_lands(app, output_root,
                                                            sentinels, elsewhere):
    # The host flips the switch, from the host.
    with client(app, host="127.0.0.1") as host:
        csrf = sign_in(host)
        # The remote client has to have been seen before it can be allowed.
        with client(app, host="192.168.1.77") as phone:
            phone_csrf = sign_in(phone)

            response = host.post("/vt/api/settings/ip",
                                 json={"ip": "192.168.1.77", "write_allowed": True},
                                 headers={"X-VT-CSRF": csrf})
            assert response.status_code == 200

            sent = phone.post(
                "/vt/api/transfer/upload?name=holiday.jpg",
                headers={"X-VT-CSRF": phone_csrf,
                         "Content-Type": "application/octet-stream"},
                content=b"\xff\xd8\xffHOLIDAY-JPEG-BYTES",
            )
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "saved"
    assert (output_root / "holiday.jpg").read_bytes() == b"\xff\xd8\xffHOLIDAY-JPEG-BYTES"
    sentinels.assert_unchanged("an allowed transfer must not disturb anything")
    sentinels.assert_no_new_files(elsewhere)


def test_duplicate_transfer_skips_and_never_overwrites(app, output_root, sentinels):
    with client(app, host="127.0.0.1") as host:
        csrf = sign_in(host)
        response = host.post(
            "/vt/api/transfer/upload?name=duplicate.jpg",
            headers={"X-VT-CSRF": csrf, "Content-Type": "application/octet-stream"},
            content=b"\xff\xd8\xffREPLACEMENT-ATTEMPT",
        )
    assert response.status_code == 200
    assert response.json()["status"] == "already_exists"
    assert (output_root / "duplicate.jpg").read_bytes() == b"\xff\xd8\xffORIGINAL-JPEG"
    sentinels.assert_unchanged("a duplicate transfer must change nothing")


@pytest.mark.parametrize("name", [
    "../escape.jpg", "/etc/escape.jpg", "sub/dir.jpg", "duplicate.jpg:evil",
    "CON.jpg", "trailing. ", "\\\\server\\share\\x.jpg",
])
def test_malicious_transfer_names_are_refused(app, name, sentinels, elsewhere):
    import urllib.parse

    with client(app, host="127.0.0.1") as host:
        csrf = sign_in(host)
        response = host.post(
            "/vt/api/transfer/upload?name=" + urllib.parse.quote(name, safe=""),
            headers={"X-VT-CSRF": csrf, "Content-Type": "application/octet-stream"},
            content=b"\xff\xd8\xffPAYLOAD",
        )
    assert response.status_code in (400, 415), f"{name} was not refused"
    sentinels.assert_unchanged(f"the refused name {name!r} must have no effect")
    sentinels.assert_no_new_files(elsewhere)


def test_unsupported_type_is_refused(app, sentinels):
    with client(app, host="127.0.0.1") as host:
        csrf = sign_in(host)
        response = host.post(
            "/vt/api/transfer/upload?name=payload.exe",
            headers={"X-VT-CSRF": csrf, "Content-Type": "application/octet-stream"},
            content=b"MZ\x90\x00",
        )
    assert response.status_code == 415
    sentinels.assert_unchanged("an unsupported type must not be written")


def test_a_file_that_lies_about_being_an_image_is_refused(app, sentinels):
    with client(app, host="127.0.0.1") as host:
        csrf = sign_in(host)
        response = host.post(
            "/vt/api/transfer/upload?name=notreally.png",
            headers={"X-VT-CSRF": csrf, "Content-Type": "application/octet-stream"},
            content=b"#!/bin/sh\nrm -rf /\n",
        )
    assert response.status_code == 415
    sentinels.assert_unchanged("a mislabelled file must not be written")


# --- save location -----------------------------------------------------------
def test_host_cannot_choose_a_save_location_inside_the_install(app, fake_root):
    with client(app, host="127.0.0.1") as host:
        csrf = sign_in(host)
        response = host.post("/vt/api/settings/save-location",
                             json={"path": str(fake_root / "cache")},
                             headers={"X-VT-CSRF": csrf})
    assert response.status_code == 400
    assert "Choose a folder outside it" in response.json()["detail"]


def test_host_cannot_choose_a_missing_save_location(app, tmp_path):
    missing = tmp_path / "nowhere"
    with client(app, host="127.0.0.1") as host:
        csrf = sign_in(host)
        response = host.post("/vt/api/settings/save-location",
                             json={"path": str(missing)},
                             headers={"X-VT-CSRF": csrf})
    assert response.status_code == 400
    assert not missing.exists(), "the app created the missing directory"


# --- media tokens ------------------------------------------------------------
def test_a_media_token_does_not_cross_sessions(app, host_tree):
    """Two signed-in clients, one token, and it works for exactly one of them."""
    with client(app, host="127.0.0.1") as host:
        sign_in(host)
        opened = host.post("/vt/api/open", json={"path": str(host_tree / "existing.mp4")},
                           headers={"X-VT-CSRF": host.cookies.get("vt_csrf")})
        if opened.status_code == 415:
            pytest.skip("ffmpeg is not available in this environment")
        assert opened.status_code == 200, opened.text
        token = opened.json()["token"]
        assert host.get(f"/vt/media/{token}").status_code in (200, 206)

        with client(app, host="192.168.1.77") as phone:
            sign_in(phone)
            stolen = phone.get(f"/vt/media/{token}",
                               headers={"accept": "application/json"})
    assert stolen.status_code == 404, "a media token was usable by another session"


def test_remote_client_cannot_open_host_paths(app, host_tree):
    with client(app, host="192.168.1.77") as phone:
        csrf = sign_in(phone)
        response = phone.post("/vt/api/open",
                              json={"path": str(host_tree / "existing.mp4")},
                              headers={"X-VT-CSRF": csrf})
        browse = phone.get("/vt/api/browse")
    assert response.status_code == 403
    assert browse.status_code == 403
