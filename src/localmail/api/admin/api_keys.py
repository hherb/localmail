# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Service layer for admin-issued API keys.

Transport-free: pure functions over a psycopg connection, no FastAPI imports.

A key is an ``api_tokens`` row with ``api_key_name`` set and ``expires_at NULL``,
minted against a dedicated **service user**. That principal is an ordinary
``api_users`` row, which is what lets the per-account ACL, ``disabled_at``, and
``sessions_invalidated_at`` reach the key with no code of their own here.

The pairing is 1:1 — one key per service user — enforced by migration 0036's
partial unique index. Everything therefore addresses a key by its principal's
id: ``api_tokens``' primary key is ``token_sha256``, which is credential
material and must never travel in a URL or a log line.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row

from localmail.api import acl
from localmail.api.admin.api_key_names import api_key_name_error
from localmail.api.auth import generate_token, hash_password, hash_token

#: Marks a raw key as one, in logs and to secret scanners. Never consulted
#: during verification — there remains exactly one lookup path.
API_KEY_PREFIX = "lmk_"


class ApiKeyFieldError(ValueError):
    """Validation rejected a create (blank name, collision, unknown account)."""


class ApiKeyNotFound(Exception):
    """No API key, or no API-key principal, with that id."""


@dataclass(frozen=True)
class CreatedKey:
    user_id: int
    name: str
    raw_key: str


@dataclass(frozen=True)
class ApiKeySummary:
    user_id: int
    name: str
    has_key: bool
    key_created_at: datetime | None
    last_used_at: datetime | None
    disabled: bool
    account_names: list[str]


def list_keys(conn: psycopg.Connection) -> list[ApiKeySummary]:
    """Every API-key principal, with its key if it currently holds one.

    Driven from ``api_users``, not from ``api_tokens``: a bot whose key was
    revoked holds no token row, and it must stay visible so an operator can
    re-key or delete it.
    """
    with conn.cursor(row_factory=class_row(ApiKeySummary)) as cur:
        cur.execute(
            "SELECT u.id AS user_id, u.username AS name, "
            "       (t.api_key_name IS NOT NULL) AS has_key, "
            "       t.created_at AS key_created_at, "
            "       t.last_used_at AS last_used_at, "
            "       (u.disabled_at IS NOT NULL) AS disabled, "
            "       COALESCE("
            "         array_agg(a.name ORDER BY a.name) "
            "           FILTER (WHERE a.name IS NOT NULL), "
            "         '{}'"
            "       ) AS account_names "
            "  FROM api_users u "
            "  LEFT JOIN api_tokens t "
            "    ON t.user_id = u.id AND t.api_key_name IS NOT NULL "
            "  LEFT JOIN user_accounts ua ON ua.user_id = u.id "
            "  LEFT JOIN accounts a ON a.id = ua.account_id "
            " WHERE u.is_service IS TRUE "
            " GROUP BY u.id, u.username, t.api_key_name, t.created_at, "
            "          t.last_used_at, u.disabled_at "
            " ORDER BY u.username"
        )
        return cur.fetchall()


def _create_service_user(conn: psycopg.Connection, name: str) -> int:
    """Insert the principal with a password hash of random bytes nobody retains.

    Rule 2 (``login_eligible_sql``) is what makes it unusable; this only makes
    the NOT NULL column satisfiable.
    """
    unusable = hash_password(secrets.token_urlsafe(32))
    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO api_users (username, password_hash, is_service) "
                "VALUES (%s, %s, TRUE) RETURNING id",
                (name, unusable),
            )
        except psycopg.errors.UniqueViolation as e:
            raise ApiKeyFieldError(f"name {name!r} is already taken") from e
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _resolve_principal(conn: psycopg.Connection, name: str) -> int:
    """Return the principal to mint against; create one if the name is free.

    Three outcomes, and the middle one is the re-key path: a service user
    holding no key is reused with its grants intact.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, is_service, "
            "       EXISTS (SELECT 1 FROM api_tokens t "
            "                WHERE t.user_id = api_users.id "
            "                  AND t.api_key_name IS NOT NULL) "
            "  FROM api_users WHERE username = %s",
            (name,),
        )
        row = cur.fetchone()
    if row is None:
        return _create_service_user(conn, name)
    user_id, is_service, has_key = int(row[0]), bool(row[1]), bool(row[2])
    if not is_service:
        raise ApiKeyFieldError(
            f"{name!r} is an existing user account, not an API key"
        )
    if has_key:
        raise ApiKeyFieldError(
            f"API key {name!r} already exists; revoke it before minting a new one"
        )
    return user_id


def create_key(
    conn: psycopg.Connection, *, name: str, account_ids: list[int],
) -> CreatedKey:
    """Mint an API key and return the raw value — the only time it exists.

    Caller commits, and must run the whole call in one transaction: a failure
    after the principal is created would otherwise leave a row that the
    operator's retry then collides with.
    """
    err = api_key_name_error(name)
    if err is not None:
        raise ApiKeyFieldError(err)
    name = name.strip()
    user_id = _resolve_principal(conn, name)
    for account_id in account_ids:
        try:
            acl.grant_account(conn, user_id, account_id)
        except psycopg.errors.ForeignKeyViolation as e:
            raise ApiKeyFieldError(f"unknown account {account_id}") from e
    raw_key = API_KEY_PREFIX + generate_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
            "VALUES (%s, %s, NULL, %s)",
            (hash_token(raw_key), user_id, name),
        )
    return CreatedKey(user_id=user_id, name=name, raw_key=raw_key)


def set_grant(
    conn: psycopg.Connection, user_id: int, account_id: int, granted: bool,
) -> None:
    """Grant or revoke one account on an API-key principal. Caller commits.

    The ``is_service`` check is what keeps this from becoming a second,
    unguarded way to edit a *person's* ACL — that belongs to
    ``users.set_grant``, which has its own guards.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM api_users WHERE id = %s AND is_service IS TRUE",
            (user_id,),
        )
        if cur.fetchone() is None:
            raise ApiKeyNotFound(f"no API-key principal with id={user_id}")
    if granted:
        try:
            acl.grant_account(conn, user_id, account_id)
        except psycopg.errors.ForeignKeyViolation as e:
            raise ApiKeyFieldError(f"unknown account {account_id}") from e
    else:
        acl.revoke_account(conn, user_id, account_id)


def revoke_key(conn: psycopg.Connection, user_id: int) -> None:
    """Delete every credential the principal holds, keeping the principal and
    its grants. Caller commits.

    Sweeps rather than deleting only the ``api_key_name IS NOT NULL`` row: under
    the 1:1 model a service user holds zero or one credential and it is always a
    key, so anything else there is a session token laundered out of the key
    before ``issue_token`` refused to mint one — and the lever has to be
    terminal on an archive that already carries one.

    The ``is_service`` predicate is what keeps the sweep from becoming a second,
    unguarded way to cut off a *person's* sessions; that belongs to
    ``users.revoke_sessions``.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_tokens t "
            " WHERE t.user_id = %s "
            "   AND EXISTS (SELECT 1 FROM api_users u "
            "                WHERE u.id = t.user_id AND u.is_service IS TRUE)",
            (user_id,),
        )
        if cur.rowcount == 0:
            raise ApiKeyNotFound(f"no API key for principal id={user_id}")


def delete_key_principal(conn: psycopg.Connection, user_id: int) -> None:
    """Delete the bot entirely; token and grants cascade. Caller commits.

    The ``is_service`` predicate is load-bearing: this is addressed by user id,
    and without it the route becomes a second way to delete a person.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_users WHERE id = %s AND is_service IS TRUE",
            (user_id,),
        )
        if cur.rowcount == 0:
            raise ApiKeyNotFound(f"no API-key principal with id={user_id}")
