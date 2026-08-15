"""Credentials, sessions and CSRF.

Passwords are stored as Argon2id hashes and never in plaintext, never in
CMD_FLAGS.txt, never in a log line. Session tokens are opaque and only their
hash is stored, so a stolen database does not yield usable cookies.

What this does *not* protect against is stated plainly rather than papered over:
over plain HTTP on a LAN the session cookie travels in the clear, and SameSite
and HttpOnly do nothing about a passive sniffer. See TRANSPORT_WARNING.
"""

import hashlib
import hmac
import secrets
import time

COOKIE_NAME = "vt_session"
CSRF_HEADER = "x-vt-csrf"

# 12 hours of idle, 7 days absolute. Long enough that a phone in a pocket is not
# constantly logged out, short enough that a forgotten tab does not live forever.
IDLE_SECONDS = 12 * 3600
ABSOLUTE_SECONDS = 7 * 24 * 3600

MIN_PASSWORD_LENGTH = 8
MIN_USERNAME_LENGTH = 1

TRANSPORT_WARNING = (
    "Video Trim protects against other people on your network using the app. "
    "It does not protect against someone who can capture traffic on your "
    "network. Use HTTPS for that."
)

HASHER_MISSING = (
    "Video Trim needs the argon2-cffi package to check passwords, and it is not "
    "installed in the venv.\n\n"
    "Your credentials are safe — they live in data/app.db, which survives a "
    "rebuild. Reinstall the dependencies and start again:\n\n"
    "    python one_click.py --update\n\n"
    "Video Trim will not start with a weaker password hash."
)


class AuthError(Exception):
    """Login refused. The message is deliberately vague about which half failed."""


class HasherUnavailable(RuntimeError):
    """argon2-cffi is missing. Never substitute a weaker scheme."""


def hasher():
    """The Argon2id hasher, or raise.

    ``data/`` survives ``--recreate`` while the venv does not, so "credential
    database present, hashing library absent" is a state you can actually reach.
    Falling back to a weaker hash there would silently downgrade every password,
    so this raises and the launcher refuses to start the listener.
    """
    try:
        from argon2 import PasswordHasher
    except Exception as exc:  # pragma: no cover - depends on the venv
        raise HasherUnavailable(HASHER_MISSING) from exc
    return PasswordHasher()


def hasher_available():
    try:
        hasher()
        return True
    except HasherUnavailable:
        return False


def hash_password(password):
    return hasher().hash(password)


def verify_password(stored_hash, password):
    """True/False, plus whether the stored hash should be upgraded."""
    from argon2.exceptions import (
        InvalidHash,
        VerificationError,
        VerifyMismatchError,
    )

    engine = hasher()
    try:
        engine.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False, False
    try:
        return True, engine.check_needs_rehash(stored_hash)
    except Exception:  # pragma: no cover - defensive
        return True, False


def validate_credentials(username, password):
    """Shared rules for setup, rotation and the CLI. Raises ValueError."""
    name = str(username or "").strip()
    if len(name) < MIN_USERNAME_LENGTH:
        raise ValueError("A username is required.")
    if len(str(password or "")) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"The password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    return name


def _token_hash(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class Session:
    """An authenticated session, as the request pipeline sees it."""

    def __init__(self, token, row, username):
        self.token = token
        self.id = row["token_hash"]
        self.user_id = int(row["user_id"])
        self.username = username
        self.csrf_token = row["csrf_token"]
        self.client_ip = row["client_ip"]
        self.host_local = bool(row["host_local"])


class AuthService:
    """Credential storage, session lifecycle and CSRF, over the SQLite store."""

    def __init__(self, store, on_session_end=None):
        self._store = store
        # Media tokens are session-scoped, so ending a session has to revoke
        # them. The registry registers itself here rather than being imported,
        # which keeps this module free of web concerns.
        self._on_session_end = on_session_end

    # --- credentials ---------------------------------------------------------
    def has_credentials(self):
        return self._store.user_count() > 0

    def create_credentials(self, username, password):
        name = validate_credentials(username, password)
        if self.has_credentials():
            raise AuthError("Credentials already exist. Use rotation instead.")
        return self._store.create_user(name, hash_password(password))

    def rotate_credentials(self, username, password, keep_token=None):
        """Change the login, revoking every other session immediately."""
        name = validate_credentials(username, password)
        user = self._store.get_user()
        if user is None:
            return self.create_credentials(name, password)
        self._store.update_user(user["id"], username=name,
                                password_hash=hash_password(password))
        keep = _token_hash(keep_token) if keep_token else None
        for token_hash in self._store.delete_sessions_for_user(user["id"], keep):
            self._end(token_hash)
        return user["id"]

    def verify(self, username, password):
        """Check a login. Raises AuthError without saying which half was wrong."""
        user = self._store.get_user(str(username or "").strip())
        if user is None:
            # Still spend the time hashing, so a missing username and a wrong
            # password do not answer at obviously different speeds.
            try:
                hash_password(password or "x")
            except Exception:
                pass
            raise AuthError("That username and password did not match.")
        ok, needs_rehash = verify_password(user["password_hash"], password or "")
        if not ok:
            raise AuthError("That username and password did not match.")
        if needs_rehash:
            self._store.update_user(user["id"], password_hash=hash_password(password))
        return user

    # --- sessions ------------------------------------------------------------
    def start_session(self, user_id, client_ip="", host_local=False):
        """Mint a fresh opaque token. Rotated on every login by construction."""
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = time.time()
        self._store.create_session(
            _token_hash(token),
            user_id,
            csrf,
            expires_at=now + IDLE_SECONDS,
            client_ip=client_ip,
            host_local=host_local,
        )
        return token, csrf

    def resolve(self, token):
        """Validate a cookie and slide the idle window. None when invalid."""
        if not token:
            return None
        token_hash = _token_hash(token)
        row = self._store.get_session(token_hash)
        if row is None:
            return None

        now = time.time()
        if now > float(row["expires_at"]):
            self._store.delete_session(token_hash)
            self._end(token_hash)
            return None
        if now - float(row["created_at"]) > ABSOLUTE_SECONDS:
            self._store.delete_session(token_hash)
            self._end(token_hash)
            return None

        self._store.touch_session(token_hash, now, now + IDLE_SECONDS)
        user = self._store.get_user_by_id(row["user_id"])
        if user is None:
            self._store.delete_session(token_hash)
            self._end(token_hash)
            return None
        return Session(token, row, user["username"])

    def end_session(self, token):
        if not token:
            return
        token_hash = _token_hash(token)
        self._store.delete_session(token_hash)
        self._end(token_hash)

    def purge_expired(self):
        for token_hash in self._store.purge_expired_sessions():
            self._end(token_hash)

    def _end(self, token_hash):
        if self._on_session_end is not None:
            try:
                self._on_session_end(token_hash)
            except Exception:  # pragma: no cover - revocation must not break logout
                pass

    # --- CSRF ----------------------------------------------------------------
    @staticmethod
    def check_csrf(session, presented):
        """Constant-time comparison against the session's own token."""
        if session is None or not presented:
            return False
        return hmac.compare_digest(str(session.csrf_token), str(presented))


class LoginThrottle:
    """Sliding-window login throttling, held in memory on purpose.

    A row per attempt would let an unauthenticated client drive unbounded disk
    writes just by guessing. Counters live here; only aggregates reach SQLite,
    and only on transitions worth recording.
    """

    WINDOW_SECONDS = 300
    MAX_FAILURES = 8
    COOLDOWN_SECONDS = 300

    def __init__(self):
        self._failures = {}
        self._locked_until = {}

    def blocked_for(self, key):
        """Seconds remaining in a cooldown, or 0."""
        until = self._locked_until.get(key, 0)
        remaining = until - time.time()
        return int(remaining) if remaining > 0 else 0

    def record_failure(self, key):
        """Returns (failure_count, just_locked)."""
        now = time.time()
        window = [t for t in self._failures.get(key, []) if now - t < self.WINDOW_SECONDS]
        window.append(now)
        self._failures[key] = window
        if len(window) >= self.MAX_FAILURES:
            already = self._locked_until.get(key, 0) > now
            self._locked_until[key] = now + self.COOLDOWN_SECONDS
            return len(window), not already
        return len(window), False

    def record_success(self, key):
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


class AccessRequestThrottle:
    """Keeps "Request access" idempotent and cheap for the host to read."""

    INTERVAL_SECONDS = 300

    def __init__(self):
        self._last = {}

    def allow(self, key):
        now = time.time()
        if now - self._last.get(key, 0) < self.INTERVAL_SECONDS:
            return False
        self._last[key] = now
        return True
