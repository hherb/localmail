"""Admin user authentication and admin-grant management.

Service layer; takes a psycopg connection. Transport-free.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from localmail.api.auth import verify_password, _DUMMY_PASSWORD_HASH
from localmail.api.errors import AuthenticationFailed


class UserNotFound(Exception):
    """No api_users row with the given username/id."""


class NotAnAdmin(Exception):
    """User exists but is_admin = FALSE."""


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
            " WHERE username = %s AND disabled_at IS NULL",
            (username,),
        )
        row = cur.fetchone()
    if row is None:
        # Constant-time path: run verify against a dummy hash to match the
        # response time of the wrong-password case (mirrors api/auth.py).
        verify_password(password, _DUMMY_PASSWORD_HASH)
        raise AuthenticationFailed("invalid username or password")
    uid, pwh, is_admin = row
    if not verify_password(password, pwh):
        raise AuthenticationFailed("invalid username or password")
    if not is_admin:
        raise NotAnAdmin()
    return AdminUser(id=int(uid), username=username)


def get_admin_user(conn: psycopg.Connection, *, user_id: int) -> AdminUser:
    """Look up an admin by id. Raises UserNotFound / NotAnAdmin.

    A disabled user (disabled_at IS NOT NULL) is treated as UserNotFound so
    the session middleware redirects to login rather than returning 403.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT username, is_admin FROM api_users"
            " WHERE id = %s AND disabled_at IS NULL",
            (user_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise UserNotFound(f"no user with id={user_id}")
    username, is_admin = row
    if not is_admin:
        raise NotAnAdmin(f"user {user_id} is not an admin")
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
