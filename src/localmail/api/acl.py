# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Per-user account ACL.

`user_accounts` is the join table; every authenticated read of account-scoped
data passes through :func:`allowed_account_ids` and feeds the resulting list
to the api-layer accessors.

`allowed_account_ids` is the canonical resolver. Routes call it once per
request; service-layer functions receive the resolved list and apply
``WHERE account_id = ANY(%s)`` at the SQL boundary.

This module is transport-free. HTTP concerns live in
:mod:`localmail.serve.routes`; CLI concerns live in :mod:`localmail.cli`.
"""

from __future__ import annotations

from datetime import datetime

import psycopg


def allowed_account_ids(conn: psycopg.Connection, user_id: int) -> list[int]:
    """Return the account IDs `user_id` may read, sorted ascending.

    Empty list means "no grants" — accessors interpret this as "404 for
    every account-scoped resource". A future admin role would override the
    list directly (e.g. "every account in the system") before passing it
    into the service-layer call; this function intentionally has no
    admin-aware branch so the admin policy stays out of the persistence
    layer.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT account_id FROM user_accounts WHERE user_id = %s "
            "ORDER BY account_id",
            (user_id,),
        )
        return [int(r[0]) for r in cur.fetchall()]


def grant_account(conn: psycopg.Connection, user_id: int, account_id: int) -> None:
    """Grant `user_id` read access to `account_id`. Idempotent.

    Raises ``psycopg.errors.ForeignKeyViolation`` if either ID does not exist.
    Caller commits.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s) "
            "ON CONFLICT (user_id, account_id) DO NOTHING",
            (user_id, account_id),
        )


def revoke_account(conn: psycopg.Connection, user_id: int, account_id: int) -> int:
    """Revoke `user_id`'s access to `account_id`. Returns the rows affected.

    Returns 0 when no grant existed — callers use the return value to
    distinguish "did nothing" from "removed a real grant" for CLI output.
    Caller commits.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM user_accounts WHERE user_id = %s AND account_id = %s",
            (user_id, account_id),
        )
        return cur.rowcount


def user_has_account(conn: psycopg.Connection, user_id: int, account_id: int) -> bool:
    """Boolean wrapper over a single-row existence check."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM user_accounts WHERE user_id = %s AND account_id = %s",
            (user_id, account_id),
        )
        return cur.fetchone() is not None


def grants_for_user(
    conn: psycopg.Connection, user_id: int,
) -> list[tuple[int, str, datetime]]:
    """Return ``(account_id, account_name, granted_at)`` for each grant, by name.

    Used by ``localmail list-api-users --with-grants``.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.id, a.name, ua.granted_at "
            "FROM user_accounts ua JOIN accounts a ON a.id = ua.account_id "
            "WHERE ua.user_id = %s ORDER BY a.name",
            (user_id,),
        )
        return [(int(aid), name, granted_at) for aid, name, granted_at in cur.fetchall()]


def resolve_user_id_by_username(
    conn: psycopg.Connection, username: str,
) -> int | None:
    """CLI helper: ``None`` for unknown user."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM api_users WHERE username = %s", (username,))
        row = cur.fetchone()
    return int(row[0]) if row is not None else None


def resolve_account_id_by_name(
    conn: psycopg.Connection, account_name: str,
) -> int | None:
    """CLI helper: ``None`` for unknown account."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE name = %s", (account_name,))
        row = cur.fetchone()
    return int(row[0]) if row is not None else None
