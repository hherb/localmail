"""Single-use authorization-code store. Codes are SHA-256-hashed; the raw code
is returned to the client once (via the redirect) and never stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg

from localmail.api.auth import generate_token, hash_token


@dataclass(frozen=True)
class CodeRow:
    client_id: str
    user_id: int
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    expires_at: datetime


def mint_code(
    conn: psycopg.Connection,
    *,
    client_id: str,
    user_id: int,
    redirect_uri: str,
    redirect_uri_provided_explicitly: bool,
    code_challenge: str,
    scopes: list[str],
    ttl_s: int,
) -> str:
    """Mint + persist a single-use code; return the raw code. Caller commits."""
    raw = generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_authorization_codes (code_sha256, client_id, "
            "user_id, redirect_uri, redirect_uri_provided_explicitly, "
            "code_challenge, scopes, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))",
            (hash_token(raw), client_id, user_id, redirect_uri,
             redirect_uri_provided_explicitly, code_challenge, scopes, ttl_s),
        )
    return raw


def load_code(conn: psycopg.Connection, raw_code: str) -> CodeRow | None:
    """Return the unexpired code row, or None. Does not consume it."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT client_id, user_id, redirect_uri, "
            "redirect_uri_provided_explicitly, code_challenge, scopes, expires_at "
            "FROM oauth_authorization_codes "
            "WHERE code_sha256 = %s AND expires_at > now()",
            (hash_token(raw_code),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return CodeRow(
        client_id=row[0],
        user_id=row[1],
        redirect_uri=row[2],
        redirect_uri_provided_explicitly=row[3],
        code_challenge=row[4],
        scopes=row[5],
        expires_at=row[6],
    )


def consume_code(conn: psycopg.Connection, raw_code: str) -> bool:
    """Delete the code; return True if a row was removed. Caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_authorization_codes WHERE code_sha256 = %s",
            (hash_token(raw_code),),
        )
        return cur.rowcount > 0
