"""Admin auth service: authenticate, get_admin_user, grant/revoke."""
from __future__ import annotations

import psycopg
import pytest

from localmail.api.admin.auth import (
    AdminUser,
    NotAnAdmin,
    UserNotFound,
    authenticate_admin,
    get_admin_user,
    grant_admin,
    revoke_admin,
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
