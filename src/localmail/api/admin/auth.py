# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Admin user authentication and admin-grant management.

Service layer; takes a psycopg connection. Transport-free.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from localmail.api.auth import DUMMY_PASSWORD_HASH, verify_password
from localmail.api.errors import AuthenticationFailed
from localmail.api.login_eligible_sql import login_eligible_sql


class UserNotFound(Exception):
    """No api_users row with the given username/id."""


class NotAnAdmin(Exception):
    """User exists but is_admin = FALSE."""


class SessionInvalidated(Exception):
    """Token's issued_at predates the user's sessions_invalidated_at.

    The operator ran `localmail revoke-admin-sessions USERNAME` after this
    token was minted; the dependency layer translates this to a redirect to
    `/admin/login` so the admin re-authenticates.
    """


@dataclass(frozen=True)
class AdminUser:
    id: int
    username: str


def authenticate_admin(
    conn: psycopg.Connection,
    *,
    username: str,
    password: str,
) -> AdminUser:
    """Verify credentials and return the admin user.

    Raises AuthenticationFailed if the username is unknown or the password
    is wrong. Raises NotAnAdmin if the credentials are valid but the user
    isn't an admin — kept distinct so the route handler can log it (a
    legitimate user trying the admin URL is different from an attacker).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, password_hash, is_admin FROM api_users"
            " WHERE username = %s AND " + login_eligible_sql(user="api_users"),
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        # Constant-time path: run verify against a dummy hash to match the
        # response time of the wrong-password case (mirrors api/auth.py).
        verify_password(password, DUMMY_PASSWORD_HASH)
        raise AuthenticationFailed("invalid username or password")
    uid, pwh, is_admin = row
    if not verify_password(password, pwh):
        raise AuthenticationFailed("invalid username or password")
    if not is_admin:
        raise NotAnAdmin()
    return AdminUser(id=int(uid), username=username)


def get_admin_user(
    conn: psycopg.Connection,
    *,
    user_id: int,
    issued_at: int | None = None,
) -> AdminUser:
    """Look up an admin by id. Raises UserNotFound / NotAnAdmin / SessionInvalidated.

    A disabled user (disabled_at IS NOT NULL) is treated as UserNotFound so
    the session middleware redirects to login rather than returning 403.

    When ``issued_at`` is supplied (Unix seconds since epoch — the
    ``SessionPayload.issued_at`` carried by the admin cookie), the row's
    ``sessions_invalidated_at`` is fetched in the same SELECT and the
    comparison ``to_timestamp(issued_at) < sessions_invalidated_at`` is
    evaluated by Postgres. Tokens minted before the revocation moment
    raise ``SessionInvalidated``. Tokens minted afterwards (or when no
    revocation has ever been issued — column is NULL) pass through.

    Existing callers that don't carry a session — `grant_admin`, CLI
    checks, tests — leave ``issued_at`` unset and skip the revocation
    check entirely, preserving today's behaviour. Passing ``None``
    sends SQL NULL; ``to_timestamp(NULL)`` is NULL and the surrounding
    boolean collapses to NULL → Python falsy, so the Python guard
    below short-circuits without raising.

    Precision boundary: ``issued_at`` is second-resolution (Unix
    epoch BIGINT) while ``sessions_invalidated_at`` is microsecond
    TIMESTAMPTZ. A token whose true mint time falls in the same
    wall-clock second as the revocation can land on either side of
    the comparison depending on truncation, biasing toward
    over-revocation (a freshly-minted token may be rejected if the
    revoke landed in the same second). That's safe: the operator
    just re-logs-in. Never the reverse — a token cannot escape
    revocation by sub-second timing.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT username,"
            "       is_admin,"
            "       (sessions_invalidated_at IS NOT NULL AND"
            "        to_timestamp(%s::bigint) < sessions_invalidated_at) "
            "  FROM api_users"
            " WHERE id = %s AND disabled_at IS NULL",
            (issued_at, user_id),
        )
        row = cur.fetchone()
    if row is None:
        raise UserNotFound(f"no user with id={user_id}")
    username, is_admin, revoked = row
    if not is_admin:
        raise NotAnAdmin(f"user {user_id} is not an admin")
    if issued_at is not None and revoked:
        raise SessionInvalidated(f"sessions revoked after token for user {user_id} was issued")
    return AdminUser(id=user_id, username=username)


def grant_admin(conn: psycopg.Connection, *, username: str) -> None:
    """Set is_admin=TRUE for the named user. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET is_admin = TRUE WHERE username = %s RETURNING id",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        raise UserNotFound(f"no user named {username!r}")
    conn.commit()


def revoke_admin(conn: psycopg.Connection, *, username: str) -> None:
    """Set is_admin=FALSE for the named user. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET is_admin = FALSE WHERE username = %s RETURNING id",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        raise UserNotFound(f"no user named {username!r}")
    conn.commit()


def revoke_admin_sessions(conn: psycopg.Connection, *, username: str) -> None:
    """Invalidate every outstanding credential for the named user.

    Sets ``sessions_invalidated_at = now()``. Three surfaces read it, all
    comparing the credential's own issue time against the cutoff:

    * **admin cookies** — rejected with :class:`SessionInvalidated`; the
      dependency layer redirects the admin back to ``/admin/login``.
    * **bearer tokens** (``api.auth.verify_token``) — every ``/v1/*``
      endpoint, ``/mcp``, and the desktop GUI stop authenticating, including
      OAuth-minted access tokens.
    * **OAuth refresh tokens** (``mcp.oauth.refresh.load_refresh``) — so a
      revoked client cannot simply mint a fresh access token and carry on.

    Effect is therefore wider than the name suggests: the user is signed out
    of the desktop client and every agent holding a token for them stops
    working until they authenticate again.

    Idempotent: a second call just bumps the timestamp forward, catching
    any token that sneaked in between the two calls. Raises
    :class:`UserNotFound` if the username is unknown — there is nothing
    to revoke and we'd rather signal the typo than silently no-op.

    Works on every ``api_users`` row, not only admins: the column lives
    on ``api_users`` and the bearer/refresh paths above already apply to
    non-admins. Refusing here would force a parallel implementation.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET sessions_invalidated_at = now()"
            " WHERE username = %s RETURNING id",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        raise UserNotFound(f"no user named {username!r}")
    conn.commit()
