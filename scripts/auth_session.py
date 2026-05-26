"""Session store + login rate limit for the admin dashboard (`/admin/ui/*`).

Two SQLite-backed primitives that the server uses to gate the admin SPA:

* ``SessionStore`` — opaque cookie token (`tm_sid`) keyed table that stores a
  hashed copy of the api key (never the plaintext), the IP / user-agent of the
  browser that issued the login, and a wall-clock expiry. A token is rotated
  per login; logout / expiry both revoke it.
* ``LoginRateLimit`` — per-IP attempt log with a sliding window. After N
  consecutive failures inside the window the IP is locked out for the rest of
  that window so naive credential stuffing is throttled at the cheapest possible
  layer.

Both helpers share a single SQLite file (created lazily at first call) inside
the server's workspace and are safe to call from FastAPI's async handlers
because every `Connection` is opened-per-call. There is no long-lived shared
connection — SQLite handles serialisation via its file lock.

The store deliberately keeps zero application context: it only knows token →
session metadata and per-IP attempts. Anything else (which api key is current,
whether the user picked light/dark theme, etc.) lives elsewhere — keeping this
module narrow lets the unit tests run in-memory in milliseconds.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# Token bytes for cookie value. 32 bytes → 256-bit entropy, URL-safe base64 ≈
# 43 chars. Plenty for an opaque session id; never traceable back to the api key.
_TOKEN_BYTES = 32

DEFAULT_SESSION_TTL_SEC = 7200           # 2h — matches design doc default
DEFAULT_LOCKOUT_COUNT = 5
DEFAULT_LOCKOUT_WINDOW_SEC = 900         # 15 minutes


def _now() -> int:
    return int(time.time())


def hash_api_key(api_key: str) -> str:
    """One-way hash used in DB rows. We never store the plaintext key — the
    cookie value is the only handle to a session, and the db row only retains a
    hash so leaking the DB does not directly leak the credential."""
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class SessionInfo:
    """In-process view of an active session row."""

    token: str
    api_key_hash: str
    expires_at: int
    ip: str
    user_agent: str


class SessionStore:
    """SQLite-backed session table: ``ui_sessions(token PK, …)``.

    Connection-per-call is intentional — SQLite is more than fast enough for
    UI traffic (single-digit ops/sec) and the per-call connection sidesteps
    every "I forgot to close the cursor in the test" footgun. ``isolation_level
    =None`` keeps the writes auto-committed; we don't need transactions for any
    of the operations here.
    """

    def __init__(self, db_path: Path, ttl_sec: int = DEFAULT_SESSION_TTL_SEC):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_sec = max(60, int(ttl_sec))
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ui_sessions (
                    token TEXT PRIMARY KEY,
                    api_key_hash TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    ip TEXT NOT NULL,
                    user_agent TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_ui_sessions_expires ON ui_sessions(expires_at)'
            )

    def create(self, api_key: str, ip: str, user_agent: str) -> SessionInfo:
        """Mint a fresh opaque token bound to (hashed api key, IP, UA)."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = _now()
        expires_at = now + self.ttl_sec
        info = SessionInfo(
            token=token,
            api_key_hash=hash_api_key(api_key),
            expires_at=expires_at,
            ip=ip or '',
            user_agent=(user_agent or '')[:512],
        )
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO ui_sessions (token, api_key_hash, expires_at, ip, user_agent, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (info.token, info.api_key_hash, info.expires_at, info.ip, info.user_agent, now),
            )
        return info

    def validate(self, token: str | None) -> SessionInfo | None:
        """Return the row iff token exists and has not expired. Lazy GC: a row
        that has lapsed gets deleted right here so the table stays bounded
        without a separate cron sweep, but a periodic ``gc()`` is still cheap
        to run and recommended on a timer."""
        if not token:
            return None
        with self._conn() as conn:
            row = conn.execute(
                'SELECT token, api_key_hash, expires_at, ip, user_agent FROM ui_sessions WHERE token = ?',
                (token,),
            ).fetchone()
            if row is None:
                return None
            if int(row['expires_at']) <= _now():
                conn.execute('DELETE FROM ui_sessions WHERE token = ?', (token,))
                return None
        return SessionInfo(
            token=row['token'],
            api_key_hash=row['api_key_hash'],
            expires_at=int(row['expires_at']),
            ip=row['ip'],
            user_agent=row['user_agent'],
        )

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._conn() as conn:
            conn.execute('DELETE FROM ui_sessions WHERE token = ?', (token,))

    def gc(self) -> int:
        """Delete every expired row; returns the count for ops visibility."""
        with self._conn() as conn:
            cur = conn.execute('DELETE FROM ui_sessions WHERE expires_at <= ?', (_now(),))
            return cur.rowcount or 0


class LoginRateLimit:
    """Per-IP failed-login throttle backed by ``login_attempts(ip, ts, success)``.

    Window logic: keep a row per attempt; on each ``check_and_record`` we count
    failures inside ``[now - window, now]`` for this IP. If the count already
    >= ``lockout_count`` *before* we record, the call refuses (returns ``False``).
    Otherwise we record the new attempt and re-evaluate so the call that finally
    crosses the threshold returns ``False`` too. Successful attempts clear the
    IP's failure history — a legitimate user typing their key wrong twice then
    correctly should not lock themselves out for the next 15 minutes.
    """

    def __init__(
        self,
        db_path: Path,
        lockout_count: int = DEFAULT_LOCKOUT_COUNT,
        window_sec: int = DEFAULT_LOCKOUT_WINDOW_SEC,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lockout_count = max(1, int(lockout_count))
        self.window_sec = max(60, int(window_sec))
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS login_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    success INTEGER NOT NULL
                )
                """
            )
            conn.execute('CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_ts ON login_attempts(ip, ts)')

    def _failures_in_window(self, conn: sqlite3.Connection, ip: str, now: int) -> int:
        row = conn.execute(
            'SELECT COUNT(*) AS c FROM login_attempts '
            'WHERE ip = ? AND success = 0 AND ts >= ?',
            (ip, now - self.window_sec),
        ).fetchone()
        return int(row['c']) if row else 0

    def is_locked(self, ip: str) -> bool:
        """Read-only check used to short-circuit the login route *before* it
        even compares the api key, so an attacker can't differentiate "wrong
        key" from "locked out" by timing the response."""
        with self._conn() as conn:
            return self._failures_in_window(conn, ip or '', _now()) >= self.lockout_count

    def check_and_record(self, ip: str, success: bool) -> bool:
        """Returns ``True`` if the attempt is allowed (whether or not the key
        was actually correct); ``False`` only when the IP was already over the
        threshold before this call.

        Side effect: appends a row for this attempt (so the *next* call sees
        the updated count). Successful attempts clear the IP's failure log."""
        ip_norm = ip or ''
        now = _now()
        with self._conn() as conn:
            failures = self._failures_in_window(conn, ip_norm, now)
            if failures >= self.lockout_count and not success:
                # Still record so admins can see the attack continued, but
                # refuse to honour the request.
                conn.execute(
                    'INSERT INTO login_attempts (ip, ts, success) VALUES (?, ?, 0)',
                    (ip_norm, now),
                )
                return False
            conn.execute(
                'INSERT INTO login_attempts (ip, ts, success) VALUES (?, ?, ?)',
                (ip_norm, now, 1 if success else 0),
            )
            if success:
                # Wipe the IP's failure history — a legit login resets the budget.
                conn.execute(
                    'DELETE FROM login_attempts WHERE ip = ? AND success = 0',
                    (ip_norm,),
                )
            return True

    def gc(self) -> int:
        """Delete rows older than the window; cheap to run periodically."""
        with self._conn() as conn:
            cur = conn.execute(
                'DELETE FROM login_attempts WHERE ts < ?',
                (_now() - self.window_sec,),
            )
            return cur.rowcount or 0


def constant_time_equals(a: str, b: str) -> bool:
    """``hmac.compare_digest`` shim that tolerates ``None`` inputs (which it
    rejects by type) — keeps the calling code branch-free."""
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode('utf-8'), b.encode('utf-8'))


def env_ttl() -> int:
    try:
        return int(os.environ.get('TM_UI_SESSION_TTL', str(DEFAULT_SESSION_TTL_SEC)))
    except ValueError:
        return DEFAULT_SESSION_TTL_SEC


def env_lockout_count() -> int:
    try:
        return int(os.environ.get('TM_UI_LOGIN_LOCKOUT_COUNT', str(DEFAULT_LOCKOUT_COUNT)))
    except ValueError:
        return DEFAULT_LOCKOUT_COUNT


def env_lockout_window() -> int:
    try:
        return int(os.environ.get('TM_UI_LOGIN_LOCKOUT_WINDOW', str(DEFAULT_LOCKOUT_WINDOW_SEC)))
    except ValueError:
        return DEFAULT_LOCKOUT_WINDOW_SEC
