"""Rotating refresh-token store. Tokens are SHA-256-hashed. Rotation deletes the
presented token and mints a fresh one with a new sliding expiry, so an active
client never needs re-authentication.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg

from localmail.api.auth import generate_token, hash_token


@dataclass(frozen=True)
class RefreshRow:
    client_id: str
    user_id: int
    scopes: list[str]
    expires_at: datetime


def mint_refresh(
    conn: psycopg.Connection,
    *,
    client_id: str,
    user_id: int,
    scopes: list[str],
    ttl_s: int,
) -> str:
    """Mint + persist a refresh token; return the raw token. Caller commits."""
    raw = generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_refresh_tokens (token_sha256, client_id, user_id, "
            "scopes, expires_at) "
            "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))",
            (hash_token(raw), client_id, user_id, scopes, ttl_s),
        )
    return raw


def load_refresh(conn: psycopg.Connection, raw_token: str) -> RefreshRow | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT client_id, user_id, scopes, expires_at "
            "FROM oauth_refresh_tokens "
            "WHERE token_sha256 = %s AND expires_at > now()",
            (hash_token(raw_token),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return RefreshRow(
        client_id=row[0], user_id=row[1], scopes=row[2], expires_at=row[3]
    )


def revoke_refresh(conn: psycopg.Connection, raw_token: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_refresh_tokens WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        return cur.rowcount > 0


def rotate_refresh(
    conn: psycopg.Connection, raw_token: str, *, ttl_s: int
) -> str | None:
    """Revoke ``raw_token`` and mint a fresh one with the same (client, user,
    scopes) and a new sliding expiry. Returns the new raw token, or None if the
    presented token was unknown/expired. Caller commits.
    """
    row = load_refresh(conn, raw_token)
    if row is None:
        return None
    revoke_refresh(conn, raw_token)
    return mint_refresh(
        conn, client_id=row.client_id, user_id=row.user_id,
        scopes=row.scopes, ttl_s=ttl_s,
    )
