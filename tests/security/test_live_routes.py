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

    def __init__(self, app, host="127.0.0.1", scheme="http"):
        import asyncio

        self._loop = asyncio.new_event_loop()
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(host, 51234)),
            # The scheme reaches the app as the request's own, exactly as
            # Uvicorn reports it for a TLS or a plain connection.
            base_url=f"{scheme}://testserver",
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


def client(app, host="127.0.0.1", scheme="http"):
    """A client whose requests appear to come from ``host``."""
    return LiveClient(app, host, scheme)


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


# --- HTTPS -------------------------------------------------------------------
def _cookie_flags(response):
    """{cookie name: [lower-cased attributes]} from a response's Set-Cookie headers."""
    flags = {}
    for header in response.headers.get_list("set-cookie"):
        name = header.split("=", 1)[0].strip()
        flags[name] = [part.strip().lower() for part in header.split(";")[1:]]
    return flags


def test_login_over_https_sets_both_cookies_secure(app):
    with client(app, scheme="https") as session:
        response = session.post("/api/login",
                                json={"username": USERNAME, "password": PASSWORD})
        assert response.status_code == 200
        assert session.get("/vt/api/capabilities").status_code == 200
    flags = _cookie_flags(response)
    assert "secure" in flags["vt_session"] and "httponly" in flags["vt_session"]
    assert "secure" in flags["vt_csrf"]


def test_login_over_plain_http_still_works_without_secure(app):
    # --http must stay a working mode: a Secure cookie over HTTP is never sent
    # back, which would make signing in impossible.
    with client(app) as session:
        response = session.post("/api/login",
                                json={"username": USERNAME, "password": PASSWORD})
        assert session.get("/vt/api/capabilities").status_code == 200
    flags = _cookie_flags(response)
    assert "secure" not in flags["vt_session"]
    assert "secure" not in flags["vt_csrf"]


def test_pages_describe_the_connection_they_arrived_on(app):
    import html

    from videotrim.security import tls
    from videotrim.security.auth import TRANSPORT_WARNING

    state = app.state.video_trim
    state.transport = tls.Transport("https", tls.MANAGED)
    with client(app, scheme="https") as session:
        login = session.get("/login").text
        sign_in(session)
        note = session.get("/vt/api/capabilities").json()["transport_note"]
        settings = session.get("/settings").text
    assert html.escape(tls.MANAGED_HTTPS_NOTE) in login
    assert html.escape(tls.MANAGED_HTTPS_NOTE) in settings
    assert note == tls.MANAGED_HTTPS_NOTE
    for page in (login, settings):
        assert html.escape(TRANSPORT_WARNING) not in page, "HTTPS page told to use HTTPS"

    state.transport = tls.Transport("http", tls.LOOPBACK)
    with client(app) as session:
        assert html.escape(tls.LOOPBACK_NOTE) in session.get("/login").text

    state.transport = tls.Transport("http", tls.PLAINTEXT)
    with client(app) as session:
        assert html.escape(TRANSPORT_WARNING) in session.get("/login").text


def test_the_private_key_and_credentials_cannot_be_opened_by_path(app, fake_root, tmp_path):
    import os

    from videotrim.security import tls

    material = tls.ensure_managed_tls("0.0.0.0", primary="192.168.1.50",
                                      hostname="host", addresses=[])
    targets = [material.key_path, material.cert_path, fake_root / "data" / "app.db"]
    if os.name == "posix":
        disguised = tmp_path / "holiday.mp4"
        disguised.symlink_to(material.key_path)
        targets.append(disguised)

    # The host itself is the most privileged browser there is.
    with client(app) as host:
        csrf = sign_in(host)
        for target in targets:
            response = host.post("/vt/api/open", json={"path": str(target)},
                                 headers={"X-VT-CSRF": csrf})
            assert response.status_code == 404, target
            assert "private data" in response.json()["detail"]
            assert "token" not in response.json()
            assert "PRIVATE KEY" not in response.text


@pytest.fixture
def https_server(app, fake_root):
    """The real app behind a real Uvicorn with the managed certificate, on loopback."""
    import socket
    import threading
    import time

    import uvicorn

    from videotrim.security import tls

    material = tls.ensure_managed_tls("127.0.0.1")
    transport = tls.Transport("https", tls.MANAGED, material)
    app.state.video_trim.transport = transport
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Lifespan off, as with the in-process clients above: these requests never
    # touch Gradio's queue, and its startup hooks are not what is under test.
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                            access_log=False, lifespan="off", **transport.uvicorn_options())
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "HTTPS server did not start"
        time.sleep(0.05)
    yield port, material
    server.should_exit = True
    thread.join(timeout=10)


def test_a_real_https_listener_end_to_end(https_server):
    import ssl

    from videotrim.security import tls

    port, material = https_server
    # Trust exactly the generated certificate: nothing is disabled, and nothing
    # process-wide changes.
    trust = ssl.create_default_context(cafile=str(material.cert_path))
    with httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=trust,
                      trust_env=False, timeout=15) as browser:
        assert browser.get("/healthz").json() == {"status": "ok"}
        login = browser.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
        assert login.status_code == 200
        flags = _cookie_flags(login)
        assert "secure" in flags["vt_session"] and "secure" in flags["vt_csrf"]
        capabilities = browser.get("/vt/api/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["transport_note"] == tls.MANAGED_HTTPS_NOTE

    # Plain HTTP to the same port is not an application session of any kind.
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                      timeout=5) as plain:
        with pytest.raises(httpx.HTTPError):
            plain.get("/healthz")


# --- saving audio only ---------------------------------------------------------
def _open_for(app, session, path):
    """Register ``path`` as this client's open video, as /vt/api/open would."""
    state = app.state.video_trim
    signed = state.auth.resolve(session.cookies.get("vt_session"))
    return state.registry.add(path, kind="source", session_id=signed.id,
                              capability="host_browse")


@pytest.fixture
def audio_ready(app, monkeypatch, host_tree):
    """ffmpeg present, the source has sound, and jobs recorded instead of run."""
    from videotrim import ffmpeg_tools

    state = app.state.video_trim
    monkeypatch.setattr(state, "ffmpeg", "ffmpeg")
    monkeypatch.setattr(ffmpeg_tools, "has_audio", lambda ffmpeg, path: True)
    started = []

    class Job:
        def snapshot(self):
            return {"id": "job1", "state": "running", "percent": 0}

    def start(kind, label, total_ms, command, target, **kwargs):
        started.append({"kind": kind, "label": label, "command": command, "target": target})
        return Job()

    monkeypatch.setattr(state.jobs, "start", start)
    return started


def test_saving_audio_starts_an_mp3_export_of_the_range(app, audio_ready, host_tree):
    with client(app) as host:
        csrf = sign_in(host)
        token = _open_for(app, host, host_tree / "existing.mp4")
        response = host.post("/vt/api/audio", json={"token": token, "a_ms": 2000, "b_ms": 7000},
                             headers={"X-VT-CSRF": csrf})
    assert response.status_code == 200, response.text
    job, = audio_ready
    assert job["kind"] == "audio"
    assert "existing_audio_00m02.0s_to_00m07.0s.mp3" in job["label"]
    assert job["target"].name == "audio.mp3"
    assert "libmp3lame" in job["command"] and "192k" in job["command"]


def test_a_video_without_sound_is_refused_plainly(app, audio_ready, host_tree, monkeypatch):
    from videotrim import ffmpeg_tools

    monkeypatch.setattr(ffmpeg_tools, "has_audio", lambda ffmpeg, path: False)
    with client(app) as host:
        csrf = sign_in(host)
        token = _open_for(app, host, host_tree / "existing.mp4")
        response = host.post("/vt/api/audio", json={"token": token, "a_ms": 0, "b_ms": 3000},
                             headers={"X-VT-CSRF": csrf})
    assert response.status_code == 422
    assert "no sound" in response.json()["detail"]
    assert not audio_ready


def test_audio_and_video_exports_never_run_at_once(app, audio_ready, host_tree, monkeypatch):
    state = app.state.video_trim
    with client(app) as host:
        csrf = sign_in(host)
        token = _open_for(app, host, host_tree / "existing.mp4")
        body = {"token": token, "a_ms": 0, "b_ms": 3000}
        monkeypatch.setattr(state.jobs, "active",
                            lambda kind=None: ["busy"] if kind == "clip" else [])
        assert host.post("/vt/api/audio", json=body,
                         headers={"X-VT-CSRF": csrf}).status_code == 409
        monkeypatch.setattr(state.jobs, "active",
                            lambda kind=None: ["busy"] if kind == "audio" else [])
        assert host.post("/vt/api/clip", json=body,
                         headers={"X-VT-CSRF": csrf}).status_code == 409
    assert not audio_ready


def test_a_new_device_cannot_save_audio_either(app, audio_ready, host_tree):
    with client(app, host="192.168.1.77") as phone:
        csrf = sign_in(phone)
        token = _open_for(app, phone, host_tree / "existing.mp4")
        response = phone.post("/vt/api/audio", json={"token": token, "a_ms": 0, "b_ms": 3000},
                              headers={"X-VT-CSRF": csrf})
    assert response.status_code == 403
    assert not audio_ready


def test_audio_needs_the_csrf_header(app, audio_ready, host_tree):
    with client(app) as host:
        sign_in(host)
        token = _open_for(app, host, host_tree / "existing.mp4")
        response = host.post("/vt/api/audio", json={"token": token, "a_ms": 0, "b_ms": 3000})
    assert response.status_code == 403
    assert not audio_ready
