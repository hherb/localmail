"""Rotating refresh-token store. Tokens are SHA-256-hashed.

Rotation tombstones the presented token (sets ``consumed_at``) and mints a
successor in the same ``family_id`` with a fresh sliding expiry, so an active
client never re-authenticates. Replaying an already-consumed token is treated as
reuse (a stolen-copy signal): the entire family is deleted (RFC 9700 §4.14.2).
"""
from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg

from localmail.api.auth import generate_token, hash_token


@dataclass(frozen=True)
class RefreshRow:
    client_id: str
    user_id: int
    scopes: list[str]
    expires_at: datetime
    family_id: _uuid.UUID


@dataclass(frozen=True)
class RotateResult:
    """Outcome of a rotation attempt.

    - ``rotated``: presented token was live; ``new_token`` holds the successor.
    - ``reuse``: presented token was an already-consumed tombstone; its family
      has been deleted. ``new_token`` is None.
    - ``unknown``: presented token was absent, expired, or its user disabled
      (no theft signal). ``new_token`` is None.
    """
    outcome: Literal["rotated", "reuse", "unknown"]
    new_token: str | None = None


def mint_refresh(
    conn: psycopg.Connection,
    *,
    client_id: str,
    user_id: int,
    scopes: list[str],
    ttl_s: int,
    family_id: _uuid.UUID | None = None,
) -> str:
    """Mint + persist a refresh token; return the raw token. Caller commits.

    ``family_id=None`` lets the DB default mint a fresh family (code-exchange);
    a supplied value joins the successor to its parent's family (rotation).
    """
    raw = generate_token()
    with conn.cursor() as cur:
        if family_id is None:
            cur.execute(
                "INSERT INTO oauth_refresh_tokens (token_sha256, client_id, "
                "user_id, scopes, expires_at) "
                "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))",
                (hash_token(raw), client_id, user_id, scopes, ttl_s),
            )
        else:
            cur.execute(
                "INSERT INTO oauth_refresh_tokens (token_sha256, client_id, "
                "user_id, scopes, expires_at, family_id) "
                "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s), %s)",
                (hash_token(raw), client_id, user_id, scopes, ttl_s, family_id),
            )
    return raw


def load_refresh(conn: psycopg.Connection, raw_token: str) -> RefreshRow | None:
    """Load a *live* (not consumed, not expired, user enabled) refresh row.

    The ``consumed_at IS NULL`` filter hides tombstones; the ``api_users`` JOIN
    + ``disabled_at IS NULL`` mirrors ``api.auth.verify_token`` (RFC 9700 §4.13).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.client_id, r.user_id, r.scopes, r.expires_at, r.family_id "
            "FROM oauth_refresh_tokens r "
            "JOIN api_users u ON u.id = r.user_id "
            "WHERE r.token_sha256 = %s AND r.expires_at > now() "
            "  AND r.consumed_at IS NULL AND u.disabled_at IS NULL",
            (hash_token(raw_token),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return RefreshRow(
        client_id=row[0], user_id=row[1], scopes=row[2],
        expires_at=row[3], family_id=row[4],
    )


def revoke_refresh(conn: psycopg.Connection, raw_token: str) -> bool:
    """Hard-delete a token by hash (the SDK's explicit revoke). Caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_refresh_tokens WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        return cur.rowcount > 0


def _raw_state(
    conn: psycopg.Connection, raw_token: str
) -> tuple[_uuid.UUID, bool] | None:
    """Return ``(family_id, is_consumed)`` for a token regardless of expiry /
    user state, or None if no such row. Used to distinguish reuse from unknown.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT family_id, consumed_at FROM oauth_refresh_tokens "
            "WHERE token_sha256 = %s",
            (hash_token(raw_token),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], row[1] is not None


def _delete_family(conn: psycopg.Connection, family_id: _uuid.UUID) -> int:
    """Hard-delete every token in a family (the reuse-detection blast radius).
    Returns the deleted count. Caller commits.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_refresh_tokens WHERE family_id = %s", (family_id,)
        )
        return cur.rowcount


def sweep_consumed(conn: psycopg.Connection) -> int:
    """Delete consumed tombstones past their own ``expires_at``. Caller commits.

    Reuse stays detectable for the full original token lifetime; afterwards the
    whole family has expired anyway, so the tombstone is safe to drop.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_refresh_tokens "
            "WHERE consumed_at IS NOT NULL AND expires_at < now()"
        )
        return cur.rowcount


def rotate_refresh(
    conn: psycopg.Connection, raw_token: str, *, ttl_s: int
) -> RotateResult:
    """Tombstone ``raw_token`` and mint a successor in the same family, or detect
    reuse and revoke the family. Caller commits.
    """
    state = _raw_state(conn, raw_token)
    if state is None:
        return RotateResult("unknown")
    family_id, is_consumed = state
    if is_consumed:
        _delete_family(conn, family_id)
        return RotateResult("reuse")
    row = load_refresh(conn, raw_token)
    if row is None:
        # present but expired / user-disabled — natural, not theft.
        return RotateResult("unknown")
    # Atomically claim the token: the ``consumed_at IS NULL`` guard makes the
    # tombstone single-writer. Under READ COMMITTED a concurrent rotation of the
    # same live token blocks on the row lock, then re-evaluates the guard against
    # the now-committed row and matches 0 rows — so exactly one caller claims it
    # and mints a successor. The loser sees the token consumed out from under it
    # (the same token presented twice = a reuse signal) and revokes the family.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_refresh_tokens SET consumed_at = now() "
            "WHERE token_sha256 = %s AND consumed_at IS NULL",
            (hash_token(raw_token),),
        )
        claimed = cur.rowcount == 1
    if not claimed:
        _delete_family(conn, row.family_id)
        return RotateResult("reuse")
    new = mint_refresh(
        conn, client_id=row.client_id, user_id=row.user_id,
        scopes=row.scopes, ttl_s=ttl_s, family_id=row.family_id,
    )
    # Opportunistic GC on the rotation path — there is no background sweeper, and
    # the table is small (bounded by live clients), so an indexed DELETE here is
    # cheaper than carrying a separate schedule.
    sweep_consumed(conn)
    return RotateResult("rotated", new_token=new)
