"""Service-layer tests for admin user management (api/admin/users.py)."""
from __future__ import annotations

import psycopg
import pytest

from localmail.api.admin import users as svc
from localmail.api.admin.auth import UserNotFound
from localmail.api.auth import verify_password


def _insert_user(conn: psycopg.Connection, username: str, *,
                 is_admin: bool = False, disabled: bool = False) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, 'x', %s) RETURNING id",
            (username, is_admin),
        )
        row = cur.fetchone()
    assert row is not None
    uid = int(row[0])
    if disabled:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE api_users SET disabled_at = now() WHERE id = %s",
                (uid,),
            )
    return uid


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


def test_get_user_with_no_accounts_has_empty_grants(db_conn):
    uid = _insert_user(db_conn, "solo")
    detail = svc.get_user(db_conn, uid)
    assert detail.account_grants == []


def test_create_user_basic(db_conn):
    uid = svc.create_user(db_conn, username="newbie", password="pw12345")
    detail = svc.get_user(db_conn, uid)
    assert detail.username == "newbie"
    assert detail.is_admin is False
    with db_conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM api_users WHERE id = %s", (uid,))
        row = cur.fetchone()
    assert row is not None and verify_password("pw12345", row[0])


def test_create_user_admin_flag(db_conn):
    uid = svc.create_user(db_conn, username="boss", password="pw12345", is_admin=True)
    assert svc.get_user(db_conn, uid).is_admin is True


def test_create_user_duplicate_username_raises_field_error(db_conn):
    svc.create_user(db_conn, username="dup", password="pw12345")
    db_conn.commit()
    with pytest.raises(svc.UserFieldError):
        svc.create_user(db_conn, username="dup", password="pw12345")
    db_conn.rollback()


@pytest.mark.parametrize("username,password", [("", "pw12345"), ("ok", "")])
def test_create_user_blank_fields_raise(db_conn, username, password):
    with pytest.raises(svc.UserFieldError):
        svc.create_user(db_conn, username=username, password=password)


def test_set_password_resets_without_old(db_conn):
    uid = _insert_user(db_conn, "amy")
    svc.set_password(db_conn, uid, "brandnew1")
    with db_conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM api_users WHERE id = %s", (uid,))
        row = cur.fetchone()
    assert row is not None and verify_password("brandnew1", row[0])


def test_set_password_blank_raises(db_conn):
    uid = _insert_user(db_conn, "amy")
    with pytest.raises(svc.UserFieldError):
        svc.set_password(db_conn, uid, "")


def test_set_password_unknown_user_raises(db_conn):
    with pytest.raises(UserNotFound):
        svc.set_password(db_conn, 999999, "whatever1")
