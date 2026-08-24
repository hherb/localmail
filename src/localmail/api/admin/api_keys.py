# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Service layer for admin-issued API keys.

Transport-free: pure functions over a psycopg connection, no FastAPI imports.

A key is an ``api_tokens`` row with ``api_key_name`` set and ``expires_at NULL``,
minted against a dedicated **service user**. That principal is an ordinary
``api_users`` row, which is what lets the per-account ACL, ``disabled_at``, and
``sessions_invalidated_at`` reach the key with no code of their own here.

The pairing is 1:1 — one key per service user — enforced by migration 0036's
partial unique index, which is what makes the principal the natural handle.
Everything therefore addresses a key by its principal's id: ``api_tokens``'
primary key is ``token_sha256``, a hash *of* the credential — not presentable
as a bearer, since verification hashes what is presented and compares, but
still not something to put in a URL or a log line.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
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
    """Validation rejected a create (blank name, collision, unknown account).

    Carries the field it is about, so the admin panel can render it beside the
    offending input instead of recovering it from the message. The message is
    the wrong channel for that: matching ``"name" in msg`` mis-filed the two
    likeliest operator errors -- reusing a person's username, and re-minting
    over a live key -- and would mis-file any future wording by accident.
    ``_form`` is the default because an error about an account id is about the
    request, not about a field of it.
    """

    def __init__(self, message: str, *, field: str = "_form") -> None:
        super().__init__(message)
        self.field = field


class ApiKeyNotFound(Exception):
    """No API key, or no API-key principal, with that id."""


@dataclass(frozen=True)
class CreatedKey:
    user_id: int
    name: str
    #: repr=False because this is the credential's only plaintext existence.
    #: The default repr renders it in full, so one `logging.info("%s", created)`
    #: or a frame-locals error reporter leaks it with nothing failing. The four
    #: call sites are disciplined; this is what makes discipline unnecessary.
    raw_key: str = field(repr=False)


@dataclass(frozen=True)
class ApiKeySummary:
    user_id: int
    name: str
    has_key: bool
    key_created_at: datetime | None
    last_used_at: datetime | None
    #: Two distinct reasons a live key stops authenticating, kept apart because
    #: the remedies differ: re-enable the principal, versus revoke and re-mint
    #: (a revoked key cannot be recovered). Reported rather than refused --
    #: both are legitimate operator actions; what was wrong was that the panel
    #: showed "active" while the bot got a bare 401 with nothing to diagnose.
    disabled: bool
    revoked: bool
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
            # A restatement of credential_valid_sql's second half, not a reuse:
            # that fragment answers "is this honoured", conflating both causes.
            # test_reported_state_matches_whether_the_key_verifies is the
            # differential pin, the ALLOWLISTED_WHERE_SQL arrangement.
            "       (t.created_at IS NOT NULL "
            "        AND u.sessions_invalidated_at IS NOT NULL "
            "        AND t.created_at < u.sessions_invalidated_at) AS revoked, "
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
            "          t.last_used_at, u.disabled_at, u.sessions_invalidated_at "
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
            raise ApiKeyFieldError(
                f"name {name!r} is already taken", field="name"
            ) from e
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _resolve_principal(conn: psycopg.Connection, name: str) -> int:
    """Return the principal to mint against; create one if the name is free.

    Four branches: mint a new principal, refuse a person's row, refuse a bot
    that still holds a key, and — last — the re-key path, where a service user
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
            f"{name!r} is an existing user account, not an API key", field="name"
        )
    if has_key:
        raise ApiKeyFieldError(
            f"API key {name!r} already exists; revoke it before minting a new one",
            field="name",
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
        raise ApiKeyFieldError(err, field="name")
    name = name.strip()
    user_id = _resolve_principal(conn, name)
    for account_id in account_ids:
        try:
            acl.grant_account(conn, user_id, account_id)
        except psycopg.errors.ForeignKeyViolation as e:
            raise ApiKeyFieldError(f"unknown account {account_id}") from e
    raw_key = API_KEY_PREFIX + generate_token()
    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
                "VALUES (%s, %s, NULL, %s)",
                (hash_token(raw_key), user_id, name),
            )
        except psycopg.errors.UniqueViolation as e:
            # _resolve_principal's has_key check and this INSERT are not atomic,
            # so a concurrent mint (a double-clicked Create button) loses here.
            # Uncaught it bypassed the routers' ApiKeyFieldError -> 400 contract
            # and surfaced as a 500, which the panel's 400/422-only swap renders
            # as an inert button. Same mapping as _create_service_user's.
            raise ApiKeyFieldError(
                f"API key {name!r} already exists; revoke it before minting a new one",
                field="name",
            ) from e
    return CreatedKey(user_id=user_id, name=name, raw_key=raw_key)


def set_grant(
    conn: psycopg.Connection, user_id: int, account_id: int, granted: bool,
) -> None:
    """Grant or revoke one account on an API-key principal. Caller commits.

    The ``is_service`` check is what keeps this from becoming a second way to
    edit a *person's* ACL; that belongs to ``users.set_grant``. Note the two are
    not symmetric — ``users.set_grant`` carries no matching guard, so it can
    edit a bot's. Both routes are admin-only, so this is a division of labour
    rather than a boundary.
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
    """Delete every ``api_tokens`` row the principal holds, keeping the
    principal and its grants. Caller commits.

    (Every credential, in practice: a service principal can hold no OAuth
    refresh token or authorization code, because Rule 2 closes the consent
    login they descend from.)

    Sweeps rather than deleting only the ``api_key_name IS NOT NULL`` row: under
    the 1:1 model a service user holds zero or one credential and it is always a
    key, so anything else there is a session token laundered out of the key
    before ``issue_token`` refused to mint one. Migration 0036 and that guard
    shipped together, so no upgrading archive can hold such a row and the sweep
    is defence in depth; note the panel's Revoke button is gated on ``has_key``
    and so would not offer it in that state — the CLI and the JSON route do.

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
