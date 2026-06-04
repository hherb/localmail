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

from localmail.api import acl
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


def would_orphan_last_admin(
    *, target_is_active_admin: bool, active_admin_count: int,
) -> bool:
    """True iff removing the target's active-admin status drops the count to 0.

    Pure. `active_admin_count` is the count of users with `is_admin IS TRUE AND
    disabled_at IS NULL`, INCLUDING the target when it currently qualifies.
    """
    return target_is_active_admin and active_admin_count <= 1


def active_admin_count(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM api_users "
            "WHERE is_admin IS TRUE AND disabled_at IS NULL"
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _user_state(conn: psycopg.Connection, user_id: int) -> tuple[bool, bool]:
    """Return (is_admin, disabled) for user_id. Raises UserNotFound."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (is_admin IS TRUE), (disabled_at IS NOT NULL) "
            "FROM api_users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise UserNotFound(f"no user with id={user_id}")
    return bool(row[0]), bool(row[1])


def _guard_not_last_admin(conn: psycopg.Connection, user_id: int, action: str) -> None:
    """Raise LastAdminError if `user_id` is the sole active admin."""
    is_admin, disabled = _user_state(conn, user_id)
    target_is_active_admin = is_admin and not disabled
    if would_orphan_last_admin(
        target_is_active_admin=target_is_active_admin,
        active_admin_count=active_admin_count(conn),
    ):
        raise LastAdminError(f"cannot {action} the last active admin")


def set_admin(conn: psycopg.Connection, user_id: int, is_admin: bool) -> None:
    """Grant/revoke admin. Revoking the last active admin raises LastAdminError."""
    if not is_admin:
        _guard_not_last_admin(conn, user_id, "demote")
    else:
        _user_state(conn, user_id)  # existence check → UserNotFound
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET is_admin = %s WHERE id = %s", (is_admin, user_id)
        )


def set_disabled(conn: psycopg.Connection, user_id: int, disabled: bool) -> None:
    """Enable/disable a user. Disabling the last active admin raises LastAdminError."""
    if disabled:
        _guard_not_last_admin(conn, user_id, "disable")
    else:
        _user_state(conn, user_id)  # existence check → UserNotFound
    with conn.cursor() as cur:
        if disabled:
            cur.execute(
                "UPDATE api_users SET disabled_at = now() WHERE id = %s", (user_id,)
            )
        else:
            cur.execute(
                "UPDATE api_users SET disabled_at = NULL WHERE id = %s", (user_id,)
            )


def delete_user(conn: psycopg.Connection, user_id: int) -> None:
    """Delete a user (tokens + grants cascade). Last active admin → LastAdminError."""
    _guard_not_last_admin(conn, user_id, "delete")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM api_users WHERE id = %s", (user_id,))
        if cur.rowcount == 0:
            raise UserNotFound(f"no user with id={user_id}")


def set_grant(
    conn: psycopg.Connection, user_id: int, account_id: int, granted: bool,
) -> None:
    """Grant or revoke `user_id`'s ACL on `account_id`. Idempotent.

    Confirms the user exists first (clean UserNotFound → 404). A bad
    account_id surfaces as UserFieldError (the grant checklist only offers
    existing accounts, so this is a defensive mapping).
    """
    _user_state(conn, user_id)  # existence check → UserNotFound
    if granted:
        try:
            acl.grant_account(conn, user_id, account_id)
        except psycopg.errors.ForeignKeyViolation as e:
            raise UserFieldError(f"unknown account {account_id}") from e
    else:
        acl.revoke_account(conn, user_id, account_id)


def revoke_sessions(conn: psycopg.Connection, user_id: int) -> None:
    """Invalidate the user's outstanding admin cookies. Raises UserNotFound."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET sessions_invalidated_at = now() WHERE id = %s",
            (user_id,),
        )
        if cur.rowcount == 0:
            raise UserNotFound(f"no user with id={user_id}")


def action_flags(
    *, target_is_active_admin: bool, active_admin_count: int, is_self: bool,
) -> dict[str, bool]:
    """Which edit-screen controls to render disabled (UX only; not enforcement).

    `block_demote` / `block_delete` fire for the logged-in admin's own row
    (self-action) or when the action would orphan the last admin.
    `block_disable` fires only on the orphan rule (self-disable is permitted).
    """
    orphan = would_orphan_last_admin(
        target_is_active_admin=target_is_active_admin,
        active_admin_count=active_admin_count,
    )
    return {
        "block_demote": is_self or orphan,
        "block_disable": orphan,
        "block_delete": is_self or orphan,
    }
