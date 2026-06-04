"""Service layer for admin-UI user management (Sub-plan 2A.4).

Transport-free: pure functions over a psycopg connection, no FastAPI imports
and no IO beyond the connection passed in. Composes the existing primitives in
api/auth.py, api/acl.py and api/admin/auth.py and adds the CRUD + guard logic
the admin screens need.

Two guards protect against admin lock-out:
  * Count-based last-admin rule — `would_orphan_last_admin` (pure) + the IO
    wrappers that read the active-admin count. Identity-agnostic, lives here.
  * Identity-based self-action rule ("you can't demote/delete yourself") — lives
    in the routers, the only layer that knows who the logged-in admin is.

`is_admin` is a non-null BOOLEAN (migration 0021), but every admin predicate uses
`is_admin IS TRUE` for safe, convention-consistent checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row

from localmail.api.admin.auth import UserNotFound
from localmail.api.auth import hash_password


class UserFieldError(ValueError):
    """Validation rejected a create / password / grant (e.g. duplicate username)."""


class LastAdminError(ValueError):
    """The action would leave the system with no active admin."""


class SelfActionError(ValueError):
    """An admin tried to demote/delete their own logged-in account.

    Raised by the routers (which know the caller's identity), never by the
    service. Defined here so both routers import it from one place.
    """


@dataclass(frozen=True)
class UserSummary:
    id: int
    username: str
    is_admin: bool
    disabled: bool
    created_at: datetime


@dataclass(frozen=True)
class AccountGrant:
    account_id: int
    account_name: str
    granted: bool


@dataclass(frozen=True)
class UserDetail:
    id: int
    username: str
    is_admin: bool
    disabled: bool
    created_at: datetime
    account_grants: list[AccountGrant]


def list_users(conn: psycopg.Connection) -> list[UserSummary]:
    """Every API user, sorted by username. `is_admin IS TRUE` (nullable-safe)."""
    with conn.cursor(row_factory=class_row(UserSummary)) as cur:
        cur.execute(
            "SELECT id, username, (is_admin IS TRUE) AS is_admin, "
            "       (disabled_at IS NOT NULL) AS disabled, created_at "
            "  FROM api_users ORDER BY username"
        )
        return cur.fetchall()


def get_user(conn: psycopg.Connection, user_id: int) -> UserDetail:
    """One user plus a grant flag for EVERY account. Raises UserNotFound."""
    with conn.cursor(row_factory=class_row(UserSummary)) as cur:
        cur.execute(
            "SELECT id, username, (is_admin IS TRUE) AS is_admin, "
            "       (disabled_at IS NOT NULL) AS disabled, created_at "
            "  FROM api_users WHERE id = %s",
            (user_id,),
        )
        user = cur.fetchone()
        if user is None:
            raise UserNotFound(f"no user with id={user_id}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.id, a.name, (ua.user_id IS NOT NULL) AS granted "
            "  FROM accounts a "
            "  LEFT JOIN user_accounts ua "
            "    ON ua.account_id = a.id AND ua.user_id = %s "
            " ORDER BY a.name",
            (user_id,),
        )
        grants = [
            AccountGrant(account_id=int(aid), account_name=name, granted=bool(granted))
            for aid, name, granted in cur.fetchall()
        ]
    return UserDetail(
        id=user.id, username=user.username, is_admin=user.is_admin,
        disabled=user.disabled, created_at=user.created_at, account_grants=grants,
    )


def _validate_new_user(username: str, password: str) -> None:
    if not username or not username.strip():
        raise UserFieldError("username must not be blank")
    if not password:
        raise UserFieldError("password must not be blank")


def create_user(
    conn: psycopg.Connection, *, username: str, password: str, is_admin: bool = False,
) -> int:
    """Insert a new api_users row and return its id.

    Reuses `auth.hash_password`; sets `is_admin` in the same INSERT. Maps a
    duplicate username to `UserFieldError` for an inline 400. Caller commits.
    """
    _validate_new_user(username, password)
    pw_hash = hash_password(password)
    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO api_users (username, password_hash, is_admin) "
                "VALUES (%s, %s, %s) RETURNING id",
                (username.strip(), pw_hash, is_admin),
            )
        except psycopg.errors.UniqueViolation as e:
            raise UserFieldError(f"username {username!r} already exists") from e
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def set_password(conn: psycopg.Connection, user_id: int, new_password: str) -> None:
    """Admin password reset — no old password required. Raises UserNotFound."""
    if not new_password:
        raise UserFieldError("password must not be blank")
    pw_hash = hash_password(new_password)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET password_hash = %s WHERE id = %s",
            (pw_hash, user_id),
        )
        if cur.rowcount == 0:
            raise UserNotFound(f"no user with id={user_id}")
