"""Service-layer tests for admin user management (api/admin/users.py)."""
from __future__ import annotations

import psycopg
import pytest

from localmail.api.admin import users as svc
from localmail.api.admin.auth import UserNotFound


def _insert_user(conn: psycopg.Connection, username: str, *,
                 is_admin: bool = False, disabled: bool = False) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin, disabled_at) "
            "VALUES (%s, 'x', %s, %s) RETURNING id",
            (username, is_admin, "now()" if disabled else None),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _insert_account(conn: psycopg.Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port, config) "
            "VALUES (%s, %s, 'password', 'imap.example', 993, '{}'::jsonb) RETURNING id",
            (name, f"{name}@b.test"),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def test_list_users_empty(db_conn):
    assert svc.list_users(db_conn) == []


def test_list_users_sorted_by_username_with_flags(db_conn):
    _insert_user(db_conn, "zoe")
    _insert_user(db_conn, "amy", is_admin=True)
    _insert_user(db_conn, "bob", disabled=True)
    rows = svc.list_users(db_conn)
    assert [r.username for r in rows] == ["amy", "bob", "zoe"]
    amy = rows[0]
    assert amy.is_admin is True and amy.disabled is False
    assert rows[1].disabled is True  # bob


def test_get_user_includes_grant_for_every_account(db_conn):
    uid = _insert_user(db_conn, "amy")
    a1 = _insert_account(db_conn, "alpha")
    a2 = _insert_account(db_conn, "beta")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
            (uid, a1),
        )
    detail = svc.get_user(db_conn, uid)
    assert detail.username == "amy"
    by_id = {g.account_id: g for g in detail.account_grants}
    assert by_id[a1].granted is True
    assert by_id[a2].granted is False
    assert by_id[a1].account_name == "alpha"


def test_get_user_unknown_raises(db_conn):
    with pytest.raises(UserNotFound):
        svc.get_user(db_conn, 999999)
