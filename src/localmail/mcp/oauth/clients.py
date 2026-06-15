"""Dynamic-client-registration store (RFC 7591). Open registration is inert
until a user logs in + consents; spam is bounded by the route rate limit and
`cleanup_unused`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg


@dataclass(frozen=True)
class ClientRow:
    client_id: str
    client_secret_sha256: bytes | None
    redirect_uris: list[str]
    client_name: str | None
    grant_types: list[str] | None
    response_types: list[str] | None
    token_endpoint_auth_method: str | None
    scope: str | None
    created_at: datetime
    last_used_at: datetime | None


def register_client(
    conn: psycopg.Connection,
    *,
    client_id: str,
    client_secret_sha256: bytes | None,
    redirect_uris: list[str],
    client_name: str | None,
    grant_types: list[str] | None,
    response_types: list[str] | None,
    token_endpoint_auth_method: str | None,
    scope: str | None,
) -> None:
    """Insert a registered client. Caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_clients (client_id, client_secret_sha256, "
            "redirect_uris, client_name, grant_types, response_types, "
            "token_endpoint_auth_method, scope) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (client_id, client_secret_sha256, redirect_uris, client_name,
             grant_types, response_types, token_endpoint_auth_method, scope),
        )


def get_client(conn: psycopg.Connection, client_id: str) -> ClientRow | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT client_id, client_secret_sha256, redirect_uris, client_name, "
            "grant_types, response_types, token_endpoint_auth_method, scope, "
            "created_at, last_used_at FROM oauth_clients WHERE client_id = %s",
            (client_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return ClientRow(
        client_id=row[0],
        client_secret_sha256=row[1],
        redirect_uris=row[2],
        client_name=row[3],
        grant_types=row[4],
        response_types=row[5],
        token_endpoint_auth_method=row[6],
        scope=row[7],
        created_at=row[8],
        last_used_at=row[9],
    )


def touch_last_used(conn: psycopg.Connection, client_id: str) -> None:
    """Mark a successful token exchange. Caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_clients SET last_used_at = now() WHERE client_id = %s",
            (client_id,),
        )


def cleanup_unused(conn: psycopg.Connection, *, retention_s: int) -> int:
    """Reap abandoned clients. Returns the deleted count. Caller commits.

    A client is abandoned when it has **no live refresh token** and its last
    activity (``last_used_at``, falling back to ``created_at`` for clients that
    never exchanged) is older than ``retention_s``. This covers both
    never-used registrations and once-used clients that idled until their
    refresh token expired. A client with any unexpired refresh token is never
    reaped — its tokens are still usable, so reaping the client row would
    silently break its next ``get_client`` lookup.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_clients c "
            "WHERE COALESCE(c.last_used_at, c.created_at) "
            "      < now() - make_interval(secs => %s) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM oauth_refresh_tokens r "
            "    WHERE r.client_id = c.client_id AND r.expires_at > now())",
            (retention_s,),
        )
        return cur.rowcount
