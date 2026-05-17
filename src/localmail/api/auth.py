"""Authentication primitives: password hashing, token issuance, verification,
and higher-level service functions (login, refresh, whoami, logout).

This module is transport-free; HTTP concerns live in localmail.serve.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError, VerificationError

from localmail.api.errors import AuthenticationFailed, InvalidToken, RateLimited

_HASHER = PasswordHasher()

# Pre-computed argon2id hash exercised on the missing-user login path so
# the response time for unknown usernames matches the verify-mismatch path.
# Without this, an unauthenticated attacker can enumerate valid usernames by
# measuring how long /v1/auth/login takes (argon2 verify is ~50-200 ms).
_DUMMY_PASSWORD_HASH = _HASHER.hash("dummy-password-for-timing-parity")


def hash_password(password: str) -> str:
    """Hash a password with argon2id. Raises ValueError on empty input."""
    if not password:
        raise ValueError("password must be non-empty")
    return _HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verify; returns False on any mismatch or malformed hash."""
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return False


TOKEN_TTL_DAYS = 30

LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60

# Global cap on /v1/auth/login attempts (all usernames, success or failure).
# Bounds the argon2 CPU work an unauthenticated attacker can induce on the
# server. Per-username limiter (above) does not help here because an attacker
# can rotate usernames; this limiter does. Tuned for a single-user local
# deployment — bump if you actually have many concurrent legit logins.
LOGIN_GLOBAL_MAX_PER_WINDOW = 30
LOGIN_GLOBAL_WINDOW_SECONDS = 60

_LOGIN_FAILURES_LOCK = threading.Lock()
_LOGIN_FAILURES: dict[str, list[float]] = {}

_LOGIN_GLOBAL_LOCK = threading.Lock()
_LOGIN_GLOBAL_ATTEMPTS: list[float] = []


def reset_login_rate_limiter() -> None:
    """Clear all per-username and global attempt history. Test-only helper."""
    with _LOGIN_FAILURES_LOCK:
        _LOGIN_FAILURES.clear()
    with _LOGIN_GLOBAL_LOCK:
        _LOGIN_GLOBAL_ATTEMPTS.clear()


def _check_login_global_rate_limit() -> None:
    cutoff = _time.monotonic() - LOGIN_GLOBAL_WINDOW_SECONDS
    with _LOGIN_GLOBAL_LOCK:
        _LOGIN_GLOBAL_ATTEMPTS[:] = [t for t in _LOGIN_GLOBAL_ATTEMPTS if t > cutoff]
        if len(_LOGIN_GLOBAL_ATTEMPTS) >= LOGIN_GLOBAL_MAX_PER_WINDOW:
            raise RateLimited(
                f"server-wide login rate limit exceeded "
                f"({LOGIN_GLOBAL_MAX_PER_WINDOW} attempts per "
                f"{LOGIN_GLOBAL_WINDOW_SECONDS}s); retry shortly"
            )
        _LOGIN_GLOBAL_ATTEMPTS.append(_time.monotonic())


def _check_login_rate_limit(username: str) -> None:
    cutoff = _time.monotonic() - LOGIN_LOCKOUT_SECONDS
    with _LOGIN_FAILURES_LOCK:
        recent = [t for t in _LOGIN_FAILURES.get(username, []) if t > cutoff]
        _LOGIN_FAILURES[username] = recent
        if len(recent) >= LOGIN_MAX_FAILURES:
            raise RateLimited(
                f"too many failed login attempts; try again in "
                f"{LOGIN_LOCKOUT_SECONDS} seconds"
            )


def _record_login_failure(username: str) -> None:
    with _LOGIN_FAILURES_LOCK:
        _LOGIN_FAILURES.setdefault(username, []).append(_time.monotonic())


def _clear_login_failures(username: str) -> None:
    with _LOGIN_FAILURES_LOCK:
        _LOGIN_FAILURES.pop(username, None)


@dataclass(frozen=True)
class AuthenticatedUser:
    """The user behind a valid bearer token."""
    id: int
    username: str


def generate_token() -> str:
    """Return a fresh 32-byte URL-safe base64 token.

    32 bytes of os.urandom = 256 bits of entropy. SHA-256 of this is
    indistinguishable from random for any feasible attacker, which is why
    `hash_token` does not need HMAC or a per-row salt.
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    """SHA-256 of the token string, returned as raw bytes for BYTEA storage."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def issue_token(
    conn: psycopg.Connection,
    user_id: int,
    *,
    ttl_days: int = TOKEN_TTL_DAYS,
) -> tuple[str, datetime]:
    """Mint a token, persist its hash, return (raw_token, expires_at).

    Caller is responsible for committing the transaction.
    """
    token = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at) VALUES (%s, %s, %s)",
            (hash_token(token), user_id, expires_at),
        )
    return token, expires_at


LAST_USED_REFRESH_SECONDS = 60


def verify_token(conn: psycopg.Connection, token: str) -> AuthenticatedUser | None:
    """Look up a bearer token; return user or None for invalid/expired/disabled.

    Updates last_used_at on success, but at most once per
    LAST_USED_REFRESH_SECONDS per token — polling clients (e.g. /v1/changes)
    would otherwise produce one DB write per request on the same row.
    """
    h = hash_token(token)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT u.id, u.username "
            "FROM api_tokens t "
            "JOIN api_users u ON u.id = t.user_id "
            "WHERE t.token_sha256 = %s "
            "  AND t.expires_at > now() "
            "  AND u.disabled_at IS NULL",
            (h,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "UPDATE api_tokens SET last_used_at = now() "
            "WHERE token_sha256 = %s "
            "  AND (last_used_at IS NULL "
            "       OR last_used_at < now() - make_interval(secs => %s))",
            (h, LAST_USED_REFRESH_SECONDS),
        )
    return AuthenticatedUser(id=row[0], username=row[1])


def create_user(conn: psycopg.Connection, username: str, password: str) -> int:
    """Insert a new api_users row. Caller commits."""
    pw_hash = hash_password(password)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES (%s, %s) RETURNING id",
            (username, pw_hash),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def login(conn: psycopg.Connection, username: str, password: str) -> tuple[str, datetime]:
    """Verify credentials and mint a token.

    Raises:
      RateLimited if either the global or per-username failure threshold was hit.
      AuthenticationFailed for bad credentials or disabled users.

    The global limit is checked before the per-username limit because the
    former protects against argon2 CPU amplification from any unauthenticated
    caller, regardless of which username they target.
    """
    _check_login_global_rate_limit()
    _check_login_rate_limit(username)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, password_hash FROM api_users "
            "WHERE username = %s AND disabled_at IS NULL",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        _record_login_failure(username)
        raise AuthenticationFailed("invalid username or password")
    if not verify_password(password, row[1]):
        _record_login_failure(username)
        raise AuthenticationFailed("invalid username or password")
    _clear_login_failures(username)
    return issue_token(conn, row[0])


def whoami(conn: psycopg.Connection, token: str) -> AuthenticatedUser:
    """Look up the user behind a token. Raises InvalidToken on failure."""
    user = verify_token(conn, token)
    if user is None:
        raise InvalidToken("token is invalid, expired, or revoked")
    return user


def logout(conn: psycopg.Connection, token: str) -> None:
    """Revoke a single token. Idempotent — bogus tokens do not raise."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM api_tokens WHERE token_sha256 = %s", (hash_token(token),))


def refresh_token(conn: psycopg.Connection, token: str) -> tuple[str, datetime]:
    """Issue a new token and revoke the presenting one atomically.

    The insert and delete happen inside the caller's open transaction. On
    commit failure the new token never becomes valid and the old one is not
    revoked, so the client can simply retry with the same bearer.
    """
    user = verify_token(conn, token)
    if user is None:
        raise InvalidToken("token is invalid, expired, or revoked")
    new_token, expires_at = issue_token(conn, user.id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM api_tokens WHERE token_sha256 = %s", (hash_token(token),))
    return new_token, expires_at
