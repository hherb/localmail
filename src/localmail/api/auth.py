"""Authentication primitives: password hashing, token issuance, verification,
and higher-level service functions (login, refresh, whoami, logout).

This module is transport-free; HTTP concerns live in localmail.serve.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time as _time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError, VerificationError

from localmail.api.errors import (
    AuthenticationFailed,
    InvalidToken,
    RateLimited,
    ValidationFailed,
)
from localmail.config import AuthConfig

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

# Hard cap on distinct usernames tracked in the per-username failure dict.
# An attacker rotating usernames past the global limiter could otherwise grow
# the dict unboundedly; once we exceed this cap we evict the oldest entry
# (LRU) so memory is bounded regardless of input pattern.
LOGIN_FAILURES_MAX_USERS = 1024

_LOGIN_FAILURES_LOCK = threading.Lock()
# OrderedDict so we can evict the least-recently-touched username on overflow.
_LOGIN_FAILURES: "OrderedDict[str, list[float]]" = OrderedDict()

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


def _sweep_login_failures_locked(now: float) -> None:
    """Drop usernames whose newest attempt is older than the lockout window.

    Caller must hold ``_LOGIN_FAILURES_LOCK``. Runs in O(n) over the dict —
    cheap because we cap size at LOGIN_FAILURES_MAX_USERS.
    """
    cutoff = now - LOGIN_LOCKOUT_SECONDS
    stale = [u for u, attempts in _LOGIN_FAILURES.items() if not attempts or attempts[-1] <= cutoff]
    for u in stale:
        _LOGIN_FAILURES.pop(u, None)


def _check_login_rate_limit(username: str) -> None:
    now = _time.monotonic()
    cutoff = now - LOGIN_LOCKOUT_SECONDS
    with _LOGIN_FAILURES_LOCK:
        recent = [t for t in _LOGIN_FAILURES.get(username, []) if t > cutoff]
        if recent:
            _LOGIN_FAILURES[username] = recent
            _LOGIN_FAILURES.move_to_end(username)
        else:
            _LOGIN_FAILURES.pop(username, None)
        if len(recent) >= LOGIN_MAX_FAILURES:
            raise RateLimited(
                f"too many failed login attempts; try again in "
                f"{LOGIN_LOCKOUT_SECONDS} seconds"
            )


def _record_login_failure(username: str) -> None:
    now = _time.monotonic()
    with _LOGIN_FAILURES_LOCK:
        attempts = _LOGIN_FAILURES.setdefault(username, [])
        attempts.append(now)
        _LOGIN_FAILURES.move_to_end(username)
        if len(_LOGIN_FAILURES) > LOGIN_FAILURES_MAX_USERS:
            _sweep_login_failures_locked(now)
            while len(_LOGIN_FAILURES) > LOGIN_FAILURES_MAX_USERS:
                _LOGIN_FAILURES.popitem(last=False)


def _clear_login_failures(username: str) -> None:
    with _LOGIN_FAILURES_LOCK:
        _LOGIN_FAILURES.pop(username, None)


def _record_login_attempt(
    conn: psycopg.Connection,
    username: str,
    client_ip: str | None,
    outcome: Literal["success", "failure"],
) -> None:
    """Append a row to api_login_attempts.

    Uses a nested SAVEPOINT so a logging failure (table missing, transient
    error) cannot abort the outer login transaction — the limiter is
    defense-in-depth, never a correctness gate for credential verification.
    """
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_login_attempts (username, ip, outcome) "
                    "VALUES (%s, %s, %s)",
                    (username, client_ip, outcome),
                )
    except psycopg.errors.CheckViolation:
        # Bad outcome label — only the internal callers can hit this; surface
        # so tests can verify the constraint. Outer transaction stays open
        # because the SAVEPOINT rolled back.
        raise
    except psycopg.Error:
        # Anything else (table missing during migration race, transient IO)
        # silently fails — better to issue a token without an audit row than
        # to deny a legit login because the audit table is unavailable.
        pass


# Stable advisory-lock key for the cleanup sweep. Any nonzero int64 works;
# choose a fixed value so all workers in the cluster contend on the same
# lock. The number itself is arbitrary — chosen for "localmail" mnemonic.
_SWEEP_ADVISORY_LOCK_KEY = 0x6C_6F_63_61_6C_6D_61_69  # "localmai" in ASCII


def _sweep_login_attempts(
    conn: psycopg.Connection,
    *,
    retention_s: int,
) -> int:
    """Best-effort DELETE of expired rows. Returns deleted row count.

    Gated by ``pg_try_advisory_lock`` so concurrent workers don't pile up
    parallel DELETEs. Returns 0 if the lock is held by another worker —
    not an error; the next worker around will get to it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(%s)", (_SWEEP_ADVISORY_LOCK_KEY,)
        )
        row = cur.fetchone()
        assert row is not None
        if not row[0]:
            return 0
        try:
            cur.execute(
                "DELETE FROM api_login_attempts "
                "WHERE ts < now() - make_interval(secs => %s)",
                (retention_s,),
            )
            return cur.rowcount
        finally:
            cur.execute(
                "SELECT pg_advisory_unlock(%s)", (_SWEEP_ADVISORY_LOCK_KEY,)
            )


def _check_login_rate_limits(
    conn: psycopg.Connection,
    username: str,
    client_ip: str | None,
    *,
    cfg: AuthConfig,
) -> None:
    """Evaluate global / per-IP / per-user caps in one round trip.

    Order is global → per-IP → per-user so a hit on the broader cap wins
    the cap label — telling the caller which knob to bump is more useful
    than reporting whichever cap was tripped first by SQL evaluation.
    """
    widest_window_s = max(
        cfg.login_global_window_s,
        cfg.login_per_ip_window_s,
        cfg.login_per_user_window_s,
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE ts > now() - make_interval(secs => %s)
              ) AS global_attempts,
              COUNT(*) FILTER (
                WHERE ip = %s
                  AND outcome = 'failure'
                  AND ts > now() - make_interval(secs => %s)
              ) AS ip_failures,
              COUNT(*) FILTER (
                WHERE username = %s
                  AND outcome = 'failure'
                  AND ts > now() - make_interval(secs => %s)
                  AND ts > COALESCE(
                    (SELECT MAX(ts) FROM api_login_attempts
                      WHERE username = %s AND outcome = 'success'),
                    '-infinity'::timestamptz
                  )
              ) AS user_failures
            FROM api_login_attempts
            WHERE ts > now() - make_interval(secs => %s)
            """,
            (
                cfg.login_global_window_s,
                client_ip, cfg.login_per_ip_window_s,
                username, cfg.login_per_user_window_s, username,
                widest_window_s,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        global_attempts, ip_failures, user_failures = row

    if global_attempts >= cfg.login_global_max:
        raise RateLimited(
            f"server-wide login rate limit exceeded "
            f"({cfg.login_global_max} attempts per {cfg.login_global_window_s}s)",
            cap="global",
            retry_after_s=cfg.login_global_window_s,
        )
    if client_ip is not None and ip_failures >= cfg.login_per_ip_max:
        raise RateLimited(
            f"too many failed logins from this IP "
            f"({cfg.login_per_ip_max} per {cfg.login_per_ip_window_s}s)",
            cap="ip",
            retry_after_s=cfg.login_per_ip_window_s,
        )
    if user_failures >= cfg.login_per_user_max:
        raise RateLimited(
            f"too many failed login attempts; try again in "
            f"{cfg.login_per_user_window_s} seconds",
            cap="user",
            retry_after_s=cfg.login_per_user_window_s,
        )


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


def change_password(
    conn: psycopg.Connection,
    user_id: int,
    old_password: str,
    new_password: str,
) -> None:
    """Replace ``user_id``'s password after verifying ``old_password``.

    Existing tokens are intentionally **not** revoked — a password rotation
    should not log the user out of their current session (the bearer is
    already proof-of-possession of the prior credential). The argon2 verify
    here runs whether or not the user exists so an attacker holding a token
    for a since-deleted user cannot enumerate that fact via response timing.

    Caller commits.

    Raises:
      ValidationFailed if ``new_password`` is empty.
      AuthenticationFailed if the user does not exist, is disabled, or
        ``old_password`` does not match.
    """
    if not new_password:
        raise ValidationFailed("new password must be non-empty")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT password_hash FROM api_users "
            "WHERE id = %s AND disabled_at IS NULL",
            (user_id,),
        )
        row = cur.fetchone()
    stored_hash = row[0] if row is not None else _DUMMY_PASSWORD_HASH
    if not verify_password(old_password, stored_hash) or row is None:
        raise AuthenticationFailed("old password is incorrect")
    new_hash = hash_password(new_password)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET password_hash = %s WHERE id = %s",
            (new_hash, user_id),
        )
