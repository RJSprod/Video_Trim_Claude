"""Authentication, sessions, CSRF, media-token ownership, and the IP write policy.

These do not spin up a real server. They exercise the units that decide, which
is where the decisions actually live — the middleware's allowlist, the token
registry's ownership check, the write policy's default-deny — so a failure
points at the rule that broke rather than at a route that happened to use it.
"""

import time
from pathlib import Path

import pytest

from videotrim.security import middleware as mw
from videotrim.security.auth import (
    AccessRequestThrottle,
    AuthError,
    AuthService,
    LoginThrottle,
    validate_credentials,
)
from videotrim.security.network import (
    HostAdminGuard,
    BrowseGuard,
    normalize_ip,
    rate_limit_key,
)
from videotrim.security.write_policy import WritePolicy
from videotrim.web.media import MediaRegistry

argon2 = pytest.importorskip("argon2", reason="argon2-cffi is a hard requirement")


# --- fakes -------------------------------------------------------------------
class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    def __init__(self, host="192.168.1.50", headers=None):
        self.client = FakeClient(host)
        self.headers = headers or {}


@pytest.fixture
def store(fake_root):
    from videotrim.config.store import Store
    return Store()


@pytest.fixture
def auth(store):
    return AuthService(store)


# --- credentials -------------------------------------------------------------
def test_password_is_never_stored_in_plaintext(store, auth):
    auth.create_credentials("hostuser", "correct horse battery")
    row = store.get_user("hostuser")
    assert "correct horse battery" not in row["password_hash"]
    assert row["password_hash"].startswith("$argon2id$")

    # Nor anywhere else in the database file.
    blob = Path(store.path).read_bytes()
    assert b"correct horse battery" not in blob


def test_short_password_is_refused():
    with pytest.raises(ValueError):
        validate_credentials("host", "short")
    with pytest.raises(ValueError):
        validate_credentials("", "a long enough password")


def test_wrong_password_and_unknown_user_report_the_same_thing(store, auth):
    auth.create_credentials("hostuser", "correct horse battery")
    with pytest.raises(AuthError) as wrong:
        auth.verify("hostuser", "wrong password here")
    with pytest.raises(AuthError) as missing:
        auth.verify("someone-else", "correct horse battery")
    assert str(wrong.value) == str(missing.value), (
        "the error distinguishes a real username from a fake one"
    )


# --- sessions ----------------------------------------------------------------
def test_session_round_trip_and_logout(store, auth):
    user = auth.create_credentials("hostuser", "correct horse battery")
    token, csrf = auth.start_session(user, client_ip="127.0.0.1", host_local=True)

    session = auth.resolve(token)
    assert session is not None and session.username == "hostuser"
    assert session.csrf_token == csrf

    auth.end_session(token)
    assert auth.resolve(token) is None


def test_only_a_hash_of_the_token_is_stored(store, auth):
    user = auth.create_credentials("hostuser", "correct horse battery")
    token, _ = auth.start_session(user)
    assert token.encode() not in Path(store.path).read_bytes()


def test_expired_session_is_rejected_and_cleared(store, auth, monkeypatch):
    user = auth.create_credentials("hostuser", "correct horse battery")
    token, _ = auth.start_session(user)
    row = store.get_session(list(store.query("SELECT token_hash FROM sessions"))[0][0])
    store.touch_session(row["token_hash"], time.time() - 10, time.time() - 1)
    assert auth.resolve(token) is None


def test_credential_change_revokes_every_other_session(store, auth):
    user = auth.create_credentials("hostuser", "correct horse battery")
    keeper, _ = auth.start_session(user, client_ip="127.0.0.1", host_local=True)
    phone, _ = auth.start_session(user, client_ip="192.168.1.50")

    auth.rotate_credentials("hostuser", "a different long password", keep_token=keeper)

    assert auth.resolve(phone) is None, "a remote session survived a password change"
    assert auth.resolve(keeper) is not None


def test_credential_change_revokes_the_media_tokens_too(store):
    registry = MediaRegistry()
    auth = AuthService(store, on_session_end=registry.revoke_session)
    user = auth.create_credentials("hostuser", "correct horse battery")
    keeper, _ = auth.start_session(user, host_local=True)
    phone, _ = auth.start_session(user, client_ip="192.168.1.50")

    phone_session = auth.resolve(phone)
    token = registry.add(Path("/tmp/whatever.mp4"), session_id=phone_session.id)
    assert registry.entry_for(token, phone_session.id) is not None

    auth.rotate_credentials("hostuser", "a different long password", keep_token=keeper)
    assert registry.entry_for(token, phone_session.id) is None, (
        "a media token outlived the session that was revoked"
    )


def test_csrf_requires_an_exact_match(store, auth):
    user = auth.create_credentials("hostuser", "correct horse battery")
    token, csrf = auth.start_session(user)
    session = auth.resolve(token)

    assert AuthService.check_csrf(session, csrf)
    assert not AuthService.check_csrf(session, csrf + "x")
    assert not AuthService.check_csrf(session, "")
    assert not AuthService.check_csrf(None, csrf)


# --- throttling --------------------------------------------------------------
def test_login_throttle_locks_out_and_keeps_counters_in_memory(store):
    throttle = LoginThrottle()
    key = "192.168.1.50"
    for _ in range(LoginThrottle.MAX_FAILURES):
        throttle.record_failure(key)
    assert throttle.blocked_for(key) > 0

    throttle.record_success(key)
    assert throttle.blocked_for(key) == 0


def test_repeated_failures_do_not_write_one_row_per_attempt(store):
    """An unauthenticated client must not be able to drive unbounded disk writes."""
    throttle = LoginThrottle()
    for _ in range(200):
        throttle.record_failure("192.168.1.50")
    rows = store.query("SELECT COUNT(*) AS n FROM auth_events")
    assert rows[0]["n"] == 0, "the in-memory throttle wrote to the database"


def test_access_requests_are_rate_limited_and_idempotent(store):
    throttle = AccessRequestThrottle()
    assert throttle.allow("192.168.1.50")
    assert not throttle.allow("192.168.1.50")


# --- IP identity -------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("::ffff:192.168.1.5", "192.168.1.5"),
    ("192.168.1.5", "192.168.1.5"),
    ("[::1]", "::1"),
    ("  10.0.0.4  ", "10.0.0.4"),
])
def test_ip_normalization_is_stable(raw, expected):
    assert normalize_ip(raw) == expected


def test_ipv6_rate_limit_keys_collapse_to_a_64(store):
    """One routed /64 must not be an unlimited supply of fresh identities."""
    a = rate_limit_key("2001:db8:1:2::1")
    b = rate_limit_key("2001:db8:1:2::dead:beef")
    assert a == b
    assert rate_limit_key("2001:db8:1:3::1") != a


# --- host admin vs browse ----------------------------------------------------
def test_allow_remote_files_never_grants_host_admin():
    guard = HostAdminGuard(addresses=frozenset({"192.168.1.10"}))
    browse = BrowseGuard(guard, allow_remote_files=True)
    remote = FakeRequest("192.168.1.99")

    assert browse.can_browse_host_paths(remote), "the read capability should widen"
    assert not guard.is_host_request(remote), (
        "--allow-remote-files promoted a remote client to host admin"
    )


def test_forwarded_headers_cannot_forge_host_status():
    guard = HostAdminGuard(addresses=frozenset())
    forged = FakeRequest("192.168.1.99", headers={"x-forwarded-for": "127.0.0.1"})
    assert not guard.is_host_request(forged)


def test_loopback_is_host_and_a_tunnel_fails_closed():
    guard = HostAdminGuard(addresses=frozenset())
    assert guard.is_host_request(FakeRequest("127.0.0.1"))

    tunnelled = HostAdminGuard(tunnel_active=True, addresses=frozenset())
    assert not tunnelled.is_host_request(FakeRequest("127.0.0.1")), (
        "an ambiguous tunnel origin must not be classified as the host"
    )


# --- write policy ------------------------------------------------------------
class FakeSession:
    id = "session-1"
    username = "hostuser"
    client_ip = "192.168.1.50"


def test_new_remote_address_is_write_denied(store):
    policy = WritePolicy(store, HostAdminGuard(addresses=frozenset()))
    decision = policy.evaluate(FakeRequest("192.168.1.50"), FakeSession())
    assert not decision.allowed
    assert decision.reason == "File transfer disabled by host"
    assert decision.can_request


def test_unauthenticated_never_gets_write_even_when_the_flag_is_set(store):
    store.set_write_allowed("192.168.1.50", True)
    policy = WritePolicy(store, HostAdminGuard(addresses=frozenset()))
    decision = policy.evaluate(FakeRequest("192.168.1.50"), None)
    assert not decision.allowed


def test_host_toggle_takes_effect_on_the_next_check(store):
    policy = WritePolicy(store, HostAdminGuard(addresses=frozenset()))
    request = FakeRequest("192.168.1.50")

    assert not policy.allowed(request, FakeSession())
    store.set_write_allowed("192.168.1.50", True)
    assert policy.allowed(request, FakeSession()), "the toggle needed a restart"
    store.set_write_allowed("192.168.1.50", False)
    assert not policy.allowed(request, FakeSession()), "revocation did not take effect"


def test_requesting_access_grants_nothing(store):
    policy = WritePolicy(store, HostAdminGuard(addresses=frozenset()))
    request = FakeRequest("192.168.1.50")
    policy.request_access(request, FakeSession(), AccessRequestThrottle())

    row = store.ip_row("192.168.1.50")
    assert row["access_requested_at"] is not None
    assert not row["write_allowed"], "requesting access granted it"
    assert not policy.allowed(request, FakeSession())


def test_authorizer_is_evaluated_late(store):
    """The gateway calls this at commit time, so revocation lands mid-job."""
    policy = WritePolicy(store, HostAdminGuard(addresses=frozenset()))
    store.set_write_allowed("192.168.1.50", True)
    check = policy.authorizer(FakeRequest("192.168.1.50"), FakeSession())

    assert check()[0] is True
    store.set_write_allowed("192.168.1.50", False)
    assert check()[0] is False


def test_ip_table_is_bounded_and_never_evicts_a_permitted_row(store):
    from videotrim.config.store import IP_HISTORY_CAP

    store.ensure_ip("10.0.0.1")
    store.set_write_allowed("10.0.0.1", True)
    store.ensure_ip("10.0.0.2")
    store.flush_ip_aggregate("10.0.0.2", successes=1, last_success=time.time())

    for index in range(IP_HISTORY_CAP + 40):
        store.ensure_ip(f"2001:db8::{index:x}")

    rows = store.query("SELECT COUNT(*) AS n FROM ip_clients")
    assert rows[0]["n"] <= IP_HISTORY_CAP + 1, "the IP table grew without bound"
    assert store.ip_row("10.0.0.1") is not None, "an allowed address was evicted"
    assert store.ip_row("10.0.0.2") is not None, "an authenticated address was evicted"


# --- media tokens ------------------------------------------------------------
def test_a_token_issued_to_one_session_is_useless_to_another(tmp_path):
    registry = MediaRegistry()
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"data")

    token = registry.add(media, session_id="session-A", capability="host_browse")
    assert registry.path_for(token, "session-A") == media.resolve()
    assert registry.path_for(token, "session-B") is None, (
        "a media token crossed sessions"
    )
    assert registry.entry_for(token, "session-B") is None


def test_the_same_path_yields_two_tokens_for_two_sessions(tmp_path):
    """No dedupe by path: sharing one token is sharing one capability."""
    registry = MediaRegistry()
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"data")

    first = registry.add(media, session_id="session-A")
    second = registry.add(media, session_id="session-B")
    assert first != second
    assert registry.path_for(first, "session-B") is None


def test_tokens_die_with_their_session(tmp_path):
    registry = MediaRegistry()
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"data")
    token = registry.add(media, session_id="session-A")

    registry.revoke_session("session-A")
    assert registry.path_for(token, "session-A") is None


# --- the middleware allowlist ------------------------------------------------
@pytest.mark.parametrize("path", ["/login", "/api/login", "/healthz",
                                  "/vt/static/login.css", "/vt/static/login.js"])
def test_public_paths(path):
    assert mw.is_public(path)


@pytest.mark.parametrize("path", [
    "/", "/settings", "/tools/media-transfer", "/vt/api/capabilities",
    "/vt/static/player.js", "/gradio_api/file/etc/passwd", "/queue/join",
    "/config", "/info", "/upload",
    # Crafted to contain an allowlisted string without being one.
    "/evil/login", "/login/../gradio_api/file", "/api/login/../../settings",
    "/vt/static/login.css/../player.js", "/healthz/../settings",
    "/loginx", "/vt/static/loginish/../../api/config",
])
def test_everything_else_is_denied(path):
    normalized = mw._normalized_path({"path": path})
    assert not mw.is_public(normalized), (
        f"{path} (normalized to {normalized}) escaped the default-deny allowlist"
    )


def test_traversal_is_collapsed_before_the_allowlist_is_consulted():
    assert mw._normalized_path({"path": "/login/../gradio_api/file"}) == "/gradio_api/file"
    assert mw._normalized_path({"path": "/%2e%2e/settings"}) == "/settings"


def test_gradio_owned_routes_are_not_public():
    """The regression test that stops a Gradio upgrade quietly reopening things."""
    for path in ("/gradio_api/file/etc/shadow", "/queue/join", "/config", "/info",
                 "/tools/video-trim", "/tools/video-trim/config"):
        assert not mw.is_public(path)
