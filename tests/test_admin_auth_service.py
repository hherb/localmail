# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Admin auth service: authenticate, get_admin_user, grant/revoke."""
from __future__ import annotations

import time

import psycopg
import pytest

from localmail.api.admin.auth import (
    AdminUser,
    NotAnAdmin,
    SessionInvalidated,
    UserNotFound,
    authenticate_admin,
    get_admin_user,
    grant_admin,
    revoke_admin,
    revoke_admin_sessions,
)
from localmail.api.auth import hash_password
from localmail.api.errors import AuthenticationFailed


def _insert_user(conn: psycopg.Connection, username: str, password: str, *, is_admin: bool) -> int:
    pwh = hash_password(password)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, %s) RETURNING id",
            (username, pwh, is_admin),
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_authenticate_admin_success(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    user = authenticate_admin(db_conn, username="horst", password="hunter2")
    assert user == AdminUser(id=uid, username="horst")


def test_authenticate_admin_wrong_password(db_conn: psycopg.Connection) -> None:
    _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    with pytest.raises(AuthenticationFailed):
        authenticate_admin(db_conn, username="horst", password="wrong")


def test_authenticate_admin_unknown_user(db_conn: psycopg.Connection) -> None:
    with pytest.raises(AuthenticationFailed):
        authenticate_admin(db_conn, username="ghost", password="any")


def test_authenticate_admin_non_admin_rejected(db_conn: psycopg.Connection) -> None:
    _insert_user(db_conn, "regular", "hunter2", is_admin=False)
    with pytest.raises(NotAnAdmin):
        authenticate_admin(db_conn, username="regular", password="hunter2")


def test_get_admin_user_success(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    assert get_admin_user(db_conn, user_id=uid) == AdminUser(id=uid, username="horst")


def test_get_admin_user_unknown(db_conn: psycopg.Connection) -> None:
    with pytest.raises(UserNotFound):
        get_admin_user(db_conn, user_id=9999)


def test_get_admin_user_non_admin(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "regular", "hunter2", is_admin=False)
    with pytest.raises(NotAnAdmin):
        get_admin_user(db_conn, user_id=uid)


def test_grant_admin_flips_flag(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "regular", "hunter2", is_admin=False)
    grant_admin(db_conn, username="regular")
    assert get_admin_user(db_conn, user_id=uid).username == "regular"


def test_grant_admin_idempotent(db_conn: psycopg.Connection) -> None:
    _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    grant_admin(db_conn, username="horst")  # already admin — no raise
    grant_admin(db_conn, username="horst")  # twice — still no raise


def test_grant_admin_unknown_user(db_conn: psycopg.Connection) -> None:
    with pytest.raises(UserNotFound):
        grant_admin(db_conn, username="ghost")


def test_revoke_admin_flips_flag(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    revoke_admin(db_conn, username="horst")
    with pytest.raises(NotAnAdmin):
        get_admin_user(db_conn, user_id=uid)


def test_revoke_admin_idempotent(db_conn: psycopg.Connection) -> None:
    _insert_user(db_conn, "regular", "hunter2", is_admin=False)
    revoke_admin(db_conn, username="regular")  # already non-admin — no raise


def test_revoke_admin_unknown_user(db_conn: psycopg.Connection) -> None:
    with pytest.raises(UserNotFound):
        revoke_admin(db_conn, username="ghost")


def test_authenticate_admin_disabled_user_rejected(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_users SET disabled_at = now() WHERE id = %s", (uid,))
    db_conn.commit()
    with pytest.raises(AuthenticationFailed):
        authenticate_admin(db_conn, username="horst", password="hunter2")


def test_get_admin_user_disabled_user_rejected(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_users SET disabled_at = now() WHERE id = %s", (uid,))
    db_conn.commit()
    with pytest.raises(UserNotFound):
        get_admin_user(db_conn, user_id=uid)


def _sessions_invalidated_at_epoch(conn: psycopg.Connection, user_id: int) -> int | None:
    """Read sessions_invalidated_at as a Unix epoch BIGINT.

    Rolls back the implicit read transaction so the next caller's now() is
    not pinned to this transaction's start time (the helper would otherwise
    silently break sleep-then-rewrite tests that rely on time advancing).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM sessions_invalidated_at)::BIGINT "
            "  FROM api_users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    conn.rollback()
    assert row is not None
    return None if row[0] is None else int(row[0])


def test_get_admin_user_without_issued_at_ignores_invalidation(db_conn: psycopg.Connection) -> None:
    """Existing behaviour: callers that don't pass issued_at skip the check.

    `grant_admin` / `revoke_admin` / smoke paths look up the user without a
    session in hand. They must keep working even on a user whose sessions
    were revoked.
    """
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    revoke_admin_sessions(db_conn, username="horst")
    assert get_admin_user(db_conn, user_id=uid).username == "horst"


def test_get_admin_user_null_invalidation_admits_any_issued_at(db_conn: psycopg.Connection) -> None:
    """Default state (column is NULL): every issued_at is accepted."""
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    now = int(time.time())
    assert get_admin_user(db_conn, user_id=uid, issued_at=now - 86400).username == "horst"
    assert get_admin_user(db_conn, user_id=uid, issued_at=0).username == "horst"


def test_get_admin_user_issued_at_after_invalidation_admits(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    revoke_admin_sessions(db_conn, username="horst")
    invalidated = _sessions_invalidated_at_epoch(db_conn, uid)
    assert invalidated is not None
    # Token issued well after the revocation moment is admitted.
    assert get_admin_user(db_conn, user_id=uid, issued_at=invalidated + 60).username == "horst"


def test_get_admin_user_issued_at_before_invalidation_raises(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    revoke_admin_sessions(db_conn, username="horst")
    invalidated = _sessions_invalidated_at_epoch(db_conn, uid)
    assert invalidated is not None
    with pytest.raises(SessionInvalidated):
        get_admin_user(db_conn, user_id=uid, issued_at=invalidated - 60)


def test_revoke_admin_sessions_sets_column(db_conn: psycopg.Connection) -> None:
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    assert _sessions_invalidated_at_epoch(db_conn, uid) is None
    revoke_admin_sessions(db_conn, username="horst")
    assert _sessions_invalidated_at_epoch(db_conn, uid) is not None


def test_revoke_admin_sessions_idempotent_advances_timestamp(db_conn: psycopg.Connection) -> None:
    """Second invocation bumps the column to a fresh now(), so tokens that
    sneaked in between the two revoke calls are also caught."""
    uid = _insert_user(db_conn, "horst", "hunter2", is_admin=True)
    revoke_admin_sessions(db_conn, username="horst")
    first = _sessions_invalidated_at_epoch(db_conn, uid)
    assert first is not None
    # Sleep one second so Postgres now() advances past the first call (timestamptz
    # has microsecond resolution; the BIGINT cast we read with does not).
    time.sleep(1.1)
    revoke_admin_sessions(db_conn, username="horst")
    second = _sessions_invalidated_at_epoch(db_conn, uid)
    assert second is not None
    assert second > first


def test_revoke_admin_sessions_unknown_user_raises(db_conn: psycopg.Connection) -> None:
    with pytest.raises(UserNotFound):
        revoke_admin_sessions(db_conn, username="ghost")


def test_revoke_admin_sessions_works_on_non_admin(db_conn: psycopg.Connection) -> None:
    """The column lives on api_users, not on admins specifically — revoking
    sessions for a regular user is meaningful for any future per-user-ACL
    cookie session, so the helper should not refuse based on is_admin.
    """
    uid = _insert_user(db_conn, "regular", "hunter2", is_admin=False)
    revoke_admin_sessions(db_conn, username="regular")
    assert _sessions_invalidated_at_epoch(db_conn, uid) is not None
