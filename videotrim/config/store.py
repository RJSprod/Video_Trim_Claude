"""SQLite storage for credentials, sessions, settings, IP history and the journal.

One file, ``data/app.db``, created 0600 inside a 0700 directory on POSIX. Every
write here is either a setting the host changed or security bookkeeping — session
rows, auth events, IP aggregates. That bookkeeping is exempt from the per-IP
write policy for an unavoidable reason: recording a failed login is itself a
write, so a rule that forbids writes for unauthenticated clients would make
login impossible to implement.
"""

import os
import sqlite3
import threading
import time
from pathlib import Path

from ..security import fs_boundary

SCHEMA_VERSION = 1

DB_NAME = "app.db"

# How many never-authenticated addresses we keep before evicting the least
# recently seen. Bounded because an attacker with a routed /64 could otherwise
# mint rows forever; surfaced in Settings so eviction is not mysterious.
IP_HISTORY_CAP = 500

_MIGRATIONS = [
    # v1 — the whole initial schema.
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        hash_scheme TEXT NOT NULL DEFAULT 'argon2id',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        csrf_token TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL,
        expires_at REAL NOT NULL,
        client_ip TEXT NOT NULL DEFAULT '',
        host_local INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ip_clients (
        ip TEXT PRIMARY KEY,
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL,
        request_count INTEGER NOT NULL DEFAULT 0,
        failed_logins INTEGER NOT NULL DEFAULT 0,
        successful_logins INTEGER NOT NULL DEFAULT 0,
        first_success REAL,
        last_success REAL,
        write_allowed INTEGER NOT NULL DEFAULT 0,
        access_requested_at REAL,
        access_requested_by TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS auth_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT NOT NULL,
        timestamp REAL NOT NULL,
        outcome TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS pending_commits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        output_dir TEXT NOT NULL,
        basename TEXT NOT NULL,
        created_at REAL NOT NULL,
        job_id TEXT NOT NULL DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_auth_events_ip ON auth_events(ip, timestamp);
    """,
]

# Auth events are useful history, not an audit log to keep forever.
_AUTH_EVENT_CAP = 2000


class Store:
    """Thread-safe access to ``data/app.db``.

    One connection guarded by a lock rather than a pool: the workload is a
    handful of small statements per request, and a single writer keeps WAL
    behaviour predictable.
    """

    def __init__(self, path=None):
        # Resolved through the module rather than captured at import time, so
        # the containment rules are read from one place at the moment they apply.
        directory = fs_boundary.ensure_internal_dir(fs_boundary.DATA_DIR, mode=0o700)
        self.path = fs_boundary.require_internal(path or (directory / DB_NAME),
                                                 what="database")
        self._lock = threading.RLock()
        fresh = not self.path.exists()
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=15
        )
        self._connection.row_factory = sqlite3.Row
        if fresh and os.name == "posix":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    # --- plumbing ------------------------------------------------------------
    def close(self):
        with self._lock:
            self._connection.close()

    def _migrate(self):
        current = 0
        try:
            row = self._connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            current = int(row["value"]) if row else 0
        except sqlite3.Error:
            current = 0

        for version in range(current, SCHEMA_VERSION):
            self._connection.executescript(_MIGRATIONS[version])
        self._connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self._connection.commit()

    def execute(self, sql, params=()):
        with self._lock:
            cursor = self._connection.execute(sql, params)
            self._connection.commit()
            return cursor

    def query(self, sql, params=()):
        with self._lock:
            return self._connection.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --- settings ------------------------------------------------------------
    def get_setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, str(value), time.time()),
        )

    def all_settings(self):
        return {row["key"]: row["value"] for row in self.query("SELECT key, value FROM settings")}

    # --- users ---------------------------------------------------------------
    def user_count(self):
        row = self.one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def get_user(self, username=None):
        if username is None:
            return self.one("SELECT * FROM users ORDER BY id LIMIT 1")
        return self.one("SELECT * FROM users WHERE username=?", (username,))

    def get_user_by_id(self, user_id):
        return self.one("SELECT * FROM users WHERE id=?", (int(user_id),))

    def create_user(self, username, password_hash, scheme="argon2id"):
        now = time.time()
        cursor = self.execute(
            "INSERT INTO users(username, password_hash, hash_scheme, created_at, updated_at) "
            "VALUES(?,?,?,?,?)",
            (username, password_hash, scheme, now, now),
        )
        return cursor.lastrowid

    def update_user(self, user_id, username=None, password_hash=None, scheme=None):
        current = self.get_user_by_id(user_id)
        if current is None:
            raise LookupError("No such user.")
        self.execute(
            "UPDATE users SET username=?, password_hash=?, hash_scheme=?, updated_at=? "
            "WHERE id=?",
            (
                username or current["username"],
                password_hash or current["password_hash"],
                scheme or current["hash_scheme"],
                time.time(),
                int(user_id),
            ),
        )

    # --- sessions ------------------------------------------------------------
    def create_session(self, token_hash, user_id, csrf_token, expires_at,
                       client_ip="", host_local=False):
        now = time.time()
        self.execute(
            "INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, "
            "last_seen, expires_at, client_ip, host_local) VALUES(?,?,?,?,?,?,?,?)",
            (token_hash, int(user_id), csrf_token, now, now, float(expires_at),
             client_ip, 1 if host_local else 0),
        )

    def get_session(self, token_hash):
        return self.one("SELECT * FROM sessions WHERE token_hash=?", (token_hash,))

    def touch_session(self, token_hash, last_seen, expires_at):
        self.execute(
            "UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?",
            (float(last_seen), float(expires_at), token_hash),
        )

    def delete_session(self, token_hash):
        self.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def delete_sessions_for_user(self, user_id, keep_token_hash=None):
        """Revoke every session for a user, optionally sparing the current one."""
        rows = self.query(
            "SELECT token_hash FROM sessions WHERE user_id=?", (int(user_id),)
        )
        revoked = []
        for row in rows:
            if keep_token_hash and row["token_hash"] == keep_token_hash:
                continue
            revoked.append(row["token_hash"])
        for token_hash in revoked:
            self.delete_session(token_hash)
        return revoked

    def purge_expired_sessions(self, now=None):
        now = time.time() if now is None else now
        rows = self.query("SELECT token_hash FROM sessions WHERE expires_at < ?", (now,))
        for row in rows:
            self.delete_session(row["token_hash"])
        return [row["token_hash"] for row in rows]

    # --- IP history ----------------------------------------------------------
    def ip_row(self, ip):
        return self.one("SELECT * FROM ip_clients WHERE ip=?", (ip,))

    def ensure_ip(self, ip):
        """Create a row for an address that reached the login endpoint.

        Only the login endpoint earns a row. A passive connection that never
        attempts authentication does not, which is what keeps this table from
        being a write primitive available to anyone who can open a socket.
        """
        now = time.time()
        existing = self.ip_row(ip)
        if existing is None:
            self._evict_ip_overflow()
            self.execute(
                "INSERT INTO ip_clients(ip, first_seen, last_seen, request_count) "
                "VALUES(?,?,?,0) ON CONFLICT(ip) DO NOTHING",
                (ip, now, now),
            )
            return self.ip_row(ip)
        return existing

    def flush_ip_aggregate(self, ip, requests=0, failures=0, successes=0,
                           last_success=None):
        """Fold in-memory counters into the permanent row.

        Called on meaningful transitions — first failure, lockout, success — not
        once per attempt, so a brute-force run cannot drive one disk write per
        guess.
        """
        now = time.time()
        self.ensure_ip(ip)
        row = self.ip_row(ip)
        first_success = row["first_success"]
        if successes and first_success is None:
            first_success = last_success or now
        self.execute(
            "UPDATE ip_clients SET last_seen=?, request_count=request_count+?, "
            "failed_logins=failed_logins+?, successful_logins=successful_logins+?, "
            "first_success=?, last_success=COALESCE(?, last_success) WHERE ip=?",
            (now, int(requests), int(failures), int(successes), first_success,
             last_success, ip),
        )

    def set_write_allowed(self, ip, allowed):
        self.ensure_ip(ip)
        self.execute(
            "UPDATE ip_clients SET write_allowed=?, access_requested_at=NULL, "
            "access_requested_by='' WHERE ip=?",
            (1 if allowed else 0, ip),
        )

    def write_allowed(self, ip):
        row = self.ip_row(ip)
        return bool(row and row["write_allowed"])

    def request_access(self, ip, username=""):
        """Record that a denied client asked. Grants nothing by itself."""
        self.ensure_ip(ip)
        row = self.ip_row(ip)
        if row and row["access_requested_at"]:
            return False  # idempotent: repeated taps do not re-queue
        self.execute(
            "UPDATE ip_clients SET access_requested_at=?, access_requested_by=? WHERE ip=?",
            (time.time(), str(username or ""), ip),
        )
        return True

    def clear_access_request(self, ip):
        self.execute(
            "UPDATE ip_clients SET access_requested_at=NULL, access_requested_by='' "
            "WHERE ip=?",
            (ip,),
        )

    def set_ip_label(self, ip, label):
        """Cosmetic annotation. Never an authorization input."""
        self.ensure_ip(ip)
        self.execute("UPDATE ip_clients SET label=? WHERE ip=?", (str(label or ""), ip))

    def list_ips(self):
        return self.query(
            "SELECT * FROM ip_clients ORDER BY "
            "CASE WHEN access_requested_at IS NULL THEN 1 ELSE 0 END, "
            "access_requested_at ASC, last_seen DESC"
        )

    def _evict_ip_overflow(self):
        """LRU-evict never-authenticated, write-denied, unlabelled rows only.

        A row that ever logged in, carries a host label, or is allowed to write
        is permanent — evicting one of those would silently revoke a permission
        the host granted.
        """
        row = self.one("SELECT COUNT(*) AS n FROM ip_clients")
        if not row or int(row["n"]) < IP_HISTORY_CAP:
            return 0
        overflow = int(row["n"]) - IP_HISTORY_CAP + 1
        victims = self.query(
            "SELECT ip FROM ip_clients WHERE successful_logins=0 AND write_allowed=0 "
            "AND label='' AND access_requested_at IS NULL "
            "ORDER BY last_seen ASC LIMIT ?",
            (overflow,),
        )
        for victim in victims:
            self.execute("DELETE FROM ip_clients WHERE ip=?", (victim["ip"],))
        return len(victims)

    # --- auth events ---------------------------------------------------------
    def record_auth_event(self, ip, outcome):
        self.execute(
            "INSERT INTO auth_events(ip, timestamp, outcome) VALUES(?,?,?)",
            (ip, time.time(), str(outcome)),
        )
        self.execute(
            "DELETE FROM auth_events WHERE id NOT IN "
            "(SELECT id FROM auth_events ORDER BY id DESC LIMIT ?)",
            (_AUTH_EVENT_CAP,),
        )

    def recent_auth_events(self, ip=None, limit=50):
        if ip:
            return self.query(
                "SELECT * FROM auth_events WHERE ip=? ORDER BY id DESC LIMIT ?",
                (ip, int(limit)),
            )
        return self.query(
            "SELECT * FROM auth_events ORDER BY id DESC LIMIT ?", (int(limit),)
        )


class CommitJournal:
    """Notes an intent to create an external file, so a crash leaves a trail.

    Report-only by design. A later run cannot prove it created an orphan, so it
    must never delete, repair or overwrite one — it can only tell the host that
    a file with that name may be incomplete.
    """

    def __init__(self, store):
        self._store = store

    def record_pending(self, output_dir, basename, job_id=""):
        """Note an intent to create. Returns the row id."""
        cursor = self._store.execute(
            "INSERT INTO pending_commits(output_dir, basename, created_at, job_id) "
            "VALUES(?,?,?,?)",
            (str(output_dir), str(basename), time.time(), str(job_id or "")),
        )
        return cursor.lastrowid

    def clear_pending(self, row_id):
        """Clear one row, by id.

        By id and never by name: an earlier run may have journalled the same
        basename, and that row is the only evidence that a file with that name
        might be incomplete. Deleting it because *this* attempt found the name
        taken would erase the warning and leave the plain "already exists"
        message in its place — which is exactly the silent-wrong-data failure
        the journal exists to prevent.
        """
        if row_id is None:
            return
        self._store.execute("DELETE FROM pending_commits WHERE id=?", (int(row_id),))

    def survivors(self):
        """Rows left over from a run that did not finish cleanly."""
        return self._store.query(
            "SELECT * FROM pending_commits ORDER BY created_at DESC"
        )

    def knows_name(self, output_dir, basename):
        row = self._store.one(
            "SELECT id FROM pending_commits WHERE output_dir=? AND basename=?",
            (str(output_dir), str(basename)),
        )
        return row is not None

    def acknowledge(self, row_id):
        """Clear a notice. Removes the row and touches nothing on disk."""
        self._store.execute("DELETE FROM pending_commits WHERE id=?", (int(row_id),))


def default_db_path():
    return Path(fs_boundary.DATA_DIR) / DB_NAME
