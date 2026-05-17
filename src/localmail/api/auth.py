"""Authentication primitives: password hashing, token issuance, verification,
and higher-level service functions (login, refresh, whoami, logout).

This module is transport-free; HTTP concerns live in localmail.serve.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError, VerificationError

from localmail.api.errors import AuthenticationFailed, InvalidToken

_HASHER = PasswordHasher()


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


@dataclass(frozen=True)
class AuthenticatedUser:
    """The user behind a valid bearer token."""
    id: int
    username: str


def generate_token() -> str:
    """Return a fresh 32-byte URL-safe base64 token (no padding)."""
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


def verify_token(conn: psycopg.Connection, token: str) -> AuthenticatedUser | None:
    """Look up a bearer token; return user or None for invalid/expired/disabled.

    Updates last_used_at on success.
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
            "UPDATE api_tokens SET last_used_at = now() WHERE token_sha256 = %s",
            (h,),
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
    """Verify credentials and mint a token. Raises AuthenticationFailed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, password_hash FROM api_users "
            "WHERE username = %s AND disabled_at IS NULL",
            (username,),
        )
        row = cur.fetchone()
    if row is None or not verify_password(password, row[1]):
        raise AuthenticationFailed("invalid username or password")
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

    Both writes happen inside whatever transaction the caller is already in,
    so a commit failure leaves both old and new state intact.
    """
    user = verify_token(conn, token)
    if user is None:
        raise InvalidToken("token is invalid, expired, or revoked")
    new_token, expires_at = issue_token(conn, user.id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM api_tokens WHERE token_sha256 = %s", (hash_token(token),))
    return new_token, expires_at
