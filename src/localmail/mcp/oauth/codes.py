# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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
    resource: str | None


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
    resource: str | None = None,
) -> str:
    """Mint + persist a single-use code; return the raw code. Caller commits."""
    raw = generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_authorization_codes (code_sha256, client_id, "
            "user_id, redirect_uri, redirect_uri_provided_explicitly, "
            "code_challenge, scopes, expires_at, resource) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, "
            "now() + make_interval(secs => %s), %s)",
            (hash_token(raw), client_id, user_id, redirect_uri,
             redirect_uri_provided_explicitly, code_challenge, scopes, ttl_s,
             resource),
        )
    return raw


def load_code(conn: psycopg.Connection, raw_code: str) -> CodeRow | None:
    """Return the unexpired code row of an enabled, non-revoked user, or None.
    Does not consume it.

    The ``api_users`` JOIN mirrors ``refresh.load_refresh`` (and through it
    ``api.auth.verify_token``), so revocation is terminal for all three
    credential kinds rather than two: exchanging a code mints an access +
    refresh pair stamped ``created_at = now()`` — past the cutoff, hence valid
    — so honouring the code would hand back exactly the credentials the
    operator just cut off. ``disabled_at`` is the same argument (RFC 9700
    §4.13). The window is only ``oauth_authorization_code_ttl_s`` (default 60 s)
    wide, but a user disabled *during* the consent round trip should fail
    closed, not complete.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.client_id, c.user_id, c.redirect_uri, "
            "c.redirect_uri_provided_explicitly, c.code_challenge, c.scopes, "
            "c.expires_at, c.resource "
            "FROM oauth_authorization_codes c "
            "JOIN api_users u ON u.id = c.user_id "
            "WHERE c.code_sha256 = %s AND c.expires_at > now() "
            "  AND u.disabled_at IS NULL "
            "  AND (u.sessions_invalidated_at IS NULL "
            "       OR c.created_at >= u.sessions_invalidated_at)",
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
        resource=row[7],
    )


def consume_code(conn: psycopg.Connection, raw_code: str) -> bool:
    """Delete the code; return True if a row was removed. Caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_authorization_codes WHERE code_sha256 = %s",
            (hash_token(raw_code),),
        )
        return cur.rowcount > 0
