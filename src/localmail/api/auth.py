"""Authentication primitives: password hashing, token issuance, verification,
and higher-level service functions (login, refresh, whoami, logout).

This module is transport-free; HTTP concerns live in localmail.serve.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time as _monotonic_time
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

logger = logging.getLogger("localmail.api.auth")

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


def reset_login_rate_limiter(conn: psycopg.Connection) -> None:
    """Truncate api_login_attempts. Test-only helper.

    Takes an explicit connection because in production we never wipe the
    audit trail; only test fixtures want a fast reset between cases.
    Caller commits.
    """
    with conn.cursor() as cur:
        cur.execute("TRUNCATE api_login_attempts RESTART IDENTITY")


def _record_login_attempt(
    conn: psycopg.Connection,
    username: str,
    client_ip: str | None,
    outcome: Literal["success", "failure"],
) -> None:
    """Append a row to api_login_attempts and commit it eagerly.

    The SAVEPOINT (``conn.transaction()``) isolates CheckViolation so a
    bad ``outcome`` label can't poison the outer transaction. The explicit
    ``conn.commit()`` after the SAVEPOINT releases is load-bearing: HTTP
    route handlers raise ``AuthenticationFailed`` on bad credentials,
    which propagates out of ``pool.connection()`` and triggers a rollback
    that would otherwise discard this audit row. Without the eager commit
    the per-user and per-IP failure caps never trip from production
    traffic (every failure rolls back). Login is read-only up to this
    point — only the audit row is in flight — so committing here is safe.
    """
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_login_attempts (username, ip, outcome) "
                    "VALUES (%s, %s, %s)",
                    (username, client_ip, outcome),
                )
        conn.commit()
    except psycopg.errors.CheckViolation:
        # Bad outcome label — only the internal callers can hit this; surface
        # so tests can verify the constraint. Outer transaction stays open
        # because the SAVEPOINT rolled back.
        raise
    except psycopg.Error as exc:
        # Table missing during migration race, transient IO, etc. — better
        # to issue a token without an audit row than to deny a legit login
        # because the audit table is unavailable. Log so an operator with
        # a chronically-broken audit table sees the signal rather than
        # silent rate-limiter loss.
        logger.warning("api_login_attempts insert failed: %s", exc)


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


# Per-worker last-sweep timestamp so cleanup runs at most once per
# AuthConfig.login_cleanup_interval_s wall-clock per process. The PG
# advisory lock in _sweep_login_attempts further dedupes across workers.
_LAST_SWEEP_AT_MONOTONIC: float = 0.0


def _maybe_sweep(conn: psycopg.Connection, cfg: AuthConfig) -> None:
    """Run the cleanup sweep if it's been >= cfg.login_cleanup_interval_s
    since this worker's last sweep, committing the DELETE eagerly.

    Like ``_record_login_attempt``, the eager commit is required because
    every login attempt eventually raises AuthenticationFailed (failure
    path) or returns through the route's outer commit (success path).
    Under failure-only traffic the route's outer rollback would discard
    the sweep DELETE and the table would grow unbounded. The advance of
    ``_LAST_SWEEP_AT_MONOTONIC`` happens BEFORE the sweep so concurrent
    requests in this process race to the advisory lock rather than all
    issuing simultaneous DELETEs.
    """
    global _LAST_SWEEP_AT_MONOTONIC
    now = _monotonic_time.monotonic()
    if now - _LAST_SWEEP_AT_MONOTONIC < cfg.login_cleanup_interval_s:
        return
    _LAST_SWEEP_AT_MONOTONIC = now
    _sweep_login_attempts(conn, retention_s=cfg.login_attempt_retention_s)
    conn.commit()


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


def login(
    conn: psycopg.Connection,
    username: str,
    password: str,
    *,
    client_ip: str | None = None,
    cfg: AuthConfig | None = None,
) -> tuple[str, datetime]:
    """Verify credentials and mint a token.

    Raises:
      RateLimited (with .cap and .retry_after_s) if any cap is exceeded.
      AuthenticationFailed for bad credentials or disabled users.

    ``cfg`` defaults to ``AuthConfig()`` so test call sites that don't
    care about thresholds still work. Production callers should pass the
    loaded ``LocalmailConfig.auth``.
    """
    if cfg is None:
        cfg = AuthConfig()
    _check_login_rate_limits(conn, username, client_ip, cfg=cfg)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, password_hash FROM api_users "
            "WHERE username = %s AND disabled_at IS NULL",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        _record_login_attempt(conn, username, client_ip, "failure")
        _maybe_sweep(conn, cfg)
        raise AuthenticationFailed("invalid username or password")
    if not verify_password(password, row[1]):
        _record_login_attempt(conn, username, client_ip, "failure")
        _maybe_sweep(conn, cfg)
        raise AuthenticationFailed("invalid username or password")
    _record_login_attempt(conn, username, client_ip, "success")
    _maybe_sweep(conn, cfg)
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
