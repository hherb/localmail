"""Unit tests for the per-user account ACL service module."""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from localmail.api.acl import (
    allowed_account_ids,
    grant_account,
    grants_for_user,
    revoke_account,
    user_has_account,
)
from localmail.api.auth import create_user


def _seed_account(conn: psycopg.Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, %s, 'password') RETURNING id",
            (name, f"{name}@example.com", "imap.example.com"),
        )
        row = cur.fetchone()
        assert row is not None
        conn.commit()
        return int(row[0])


def _seed_user(conn: psycopg.Connection, username: str) -> int:
    uid = create_user(conn, username, "hunter2")
    conn.commit()
    return uid


def test_grant_account_inserts_row(db_conn):
    uid = _seed_user(db_conn, "alice")
    aid = _seed_account(db_conn, "gmail-work")
    grant_account(db_conn, uid, aid)
    db_conn.commit()
    assert user_has_account(db_conn, uid, aid) is True


def test_grant_account_idempotent(db_conn):
    uid = _seed_user(db_conn, "alice")
    aid = _seed_account(db_conn, "gmail-work")
    grant_account(db_conn, uid, aid)
    db_conn.commit()
    grant_account(db_conn, uid, aid)
    db_conn.commit()
    assert allowed_account_ids(db_conn, uid) == [aid]


def test_revoke_account_returns_affected_count(db_conn):
    uid = _seed_user(db_conn, "alice")
    aid = _seed_account(db_conn, "gmail-work")
    grant_account(db_conn, uid, aid)
    db_conn.commit()
    affected = revoke_account(db_conn, uid, aid)
    db_conn.commit()
    assert affected == 1
    assert user_has_account(db_conn, uid, aid) is False


def test_revoke_account_missing_grant_returns_zero(db_conn):
    uid = _seed_user(db_conn, "alice")
    aid = _seed_account(db_conn, "gmail-work")
    affected = revoke_account(db_conn, uid, aid)
    db_conn.commit()
    assert affected == 0


def test_allowed_account_ids_empty_for_user_without_grants(db_conn):
    uid = _seed_user(db_conn, "alice")
    _seed_account(db_conn, "gmail-work")
    assert allowed_account_ids(db_conn, uid) == []


def test_allowed_account_ids_returns_sorted_ids(db_conn):
    uid = _seed_user(db_conn, "alice")
    a1 = _seed_account(db_conn, "alpha")
    a2 = _seed_account(db_conn, "beta")
    a3 = _seed_account(db_conn, "gamma")
    grant_account(db_conn, uid, a2)
    grant_account(db_conn, uid, a3)
    grant_account(db_conn, uid, a1)
    db_conn.commit()
    assert allowed_account_ids(db_conn, uid) == sorted([a1, a2, a3])


def test_grants_for_user_returns_account_metadata(db_conn):
    uid = _seed_user(db_conn, "alice")
    aid = _seed_account(db_conn, "gmail-work")
    grant_account(db_conn, uid, aid)
    db_conn.commit()
    grants = grants_for_user(db_conn, uid)
    assert len(grants) == 1
    g_account_id, g_account_name, g_granted_at = grants[0]
    assert g_account_id == aid
    assert g_account_name == "gmail-work"
    assert isinstance(g_granted_at, datetime)
    assert g_granted_at.tzinfo is not None


def test_user_has_account_handles_unknown_pair(db_conn):
    uid = _seed_user(db_conn, "alice")
    aid = _seed_account(db_conn, "gmail-work")
    assert user_has_account(db_conn, uid, aid) is False
    assert user_has_account(db_conn, 999999, aid) is False
    assert user_has_account(db_conn, uid, 999999) is False


def test_grant_cascades_on_user_delete(db_conn):
    uid = _seed_user(db_conn, "alice")
    aid = _seed_account(db_conn, "gmail-work")
    grant_account(db_conn, uid, aid)
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM api_users WHERE id = %s", (uid,))
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM user_accounts WHERE user_id = %s", (uid,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


def test_grant_cascades_on_account_delete(db_conn):
    uid = _seed_user(db_conn, "alice")
    aid = _seed_account(db_conn, "gmail-work")
    grant_account(db_conn, uid, aid)
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE id = %s", (aid,))
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM user_accounts WHERE account_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


def test_grant_unknown_user_raises(db_conn):
    aid = _seed_account(db_conn, "gmail-work")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        grant_account(db_conn, 999999, aid)


def test_grant_unknown_account_raises(db_conn):
    uid = _seed_user(db_conn, "alice")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        grant_account(db_conn, uid, 999999)
