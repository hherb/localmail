# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Service layer for admin-issued API keys."""
from __future__ import annotations

import psycopg
import pytest

from localmail.api.admin import api_keys as svc
from localmail.api.admin.api_key_names import api_key_name_error
from localmail.api.auth import hash_password, verify_token


def _account(conn: psycopg.Connection, name: str) -> int:
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


def test_name_validation_is_pure_and_message_shaped():
    assert api_key_name_error("bot") is None
    assert api_key_name_error("") == "name must not be blank"
    assert api_key_name_error("   ") == "name must not be blank"
    assert "longer than" in (api_key_name_error("x" * 129) or "")


def test_create_key_mints_a_working_credential(db_conn):
    aid = _account(db_conn, "work")
    created = svc.create_key(db_conn, name="my_mail_bot", account_ids=[aid])
    db_conn.commit()
    assert created.raw_key.startswith(svc.API_KEY_PREFIX)
    user = verify_token(db_conn, created.raw_key)
    assert user is not None
    assert user.id == created.user_id
    assert user.is_api_key is True


def test_the_principal_is_a_service_user_and_never_admin(db_conn):
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT username, is_service, is_admin FROM api_users WHERE id = %s",
            (created.user_id,),
        )
        row = cur.fetchone()
    assert row == ("bot", True, False)


def test_grants_are_applied(db_conn):
    aid = _account(db_conn, "work")
    created = svc.create_key(db_conn, name="bot", account_ids=[aid])
    db_conn.commit()
    rows = svc.list_keys(db_conn)
    assert [r.account_names for r in rows] == [["work"]]


def test_a_human_username_is_refused(db_conn):
    """Rule 1's front door: minting a key named after a human admin would hand
    out an admin credential."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('root', %s, TRUE)",
            (hash_password("pw"),),
        )
    db_conn.commit()
    with pytest.raises(svc.ApiKeyFieldError):
        svc.create_key(db_conn, name="root", account_ids=[])
    db_conn.rollback()


def test_minting_twice_for_a_live_bot_is_refused(db_conn):
    svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    with pytest.raises(svc.ApiKeyFieldError) as exc:
        svc.create_key(db_conn, name="bot", account_ids=[])
    assert "revoke" in str(exc.value)
    db_conn.rollback()


def test_revoke_keeps_the_principal_and_re_keying_keeps_the_grants(db_conn):
    """The whole reason revoke and delete are separate operations."""
    aid = _account(db_conn, "work")
    first = svc.create_key(db_conn, name="bot", account_ids=[aid])
    db_conn.commit()
    svc.revoke_key(db_conn, first.user_id)
    db_conn.commit()
    assert verify_token(db_conn, first.raw_key) is None

    rows = svc.list_keys(db_conn)
    assert len(rows) == 1
    assert rows[0].has_key is False
    assert rows[0].account_names == ["work"]

    second = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    assert second.user_id == first.user_id
    assert second.raw_key != first.raw_key
    assert svc.list_keys(db_conn)[0].account_names == ["work"]


def test_delete_principal_removes_the_bot_and_its_grants(db_conn):
    aid = _account(db_conn, "work")
    created = svc.create_key(db_conn, name="bot", account_ids=[aid])
    db_conn.commit()
    svc.delete_key_principal(db_conn, created.user_id)
    db_conn.commit()
    assert svc.list_keys(db_conn) == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM user_accounts")
        row = cur.fetchone()
    assert row is not None and row[0] == 0


def test_delete_principal_refuses_a_human_user(db_conn):
    """The route addresses principals by id; it must not become a way to delete
    a person."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES ('root', 'x') "
            "RETURNING id"
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    with pytest.raises(svc.ApiKeyNotFound):
        svc.delete_key_principal(db_conn, int(row[0]))


def test_revoke_unknown_is_not_found(db_conn):
    with pytest.raises(svc.ApiKeyNotFound):
        svc.revoke_key(db_conn, 999999)


def test_an_unknown_account_is_a_field_error(db_conn):
    with pytest.raises(svc.ApiKeyFieldError):
        svc.create_key(db_conn, name="bot", account_ids=[999999])
    db_conn.rollback()


def test_list_never_carries_the_raw_key(db_conn):
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    rendered = repr(svc.list_keys(db_conn))
    assert created.raw_key not in rendered


def test_the_raw_key_is_stored_nowhere(db_conn):
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT token_sha256, api_key_name FROM api_tokens")
        rows = cur.fetchall()
    assert created.raw_key.encode() not in bytes(rows[0][0])
    assert rows[0][1] == "bot"


def test_set_grant_refuses_a_human_principal(db_conn):
    """Otherwise this is a second, unguarded way to edit a person's ACL."""
    aid = _account(db_conn, "work")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES ('root', 'x') "
            "RETURNING id"
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    with pytest.raises(svc.ApiKeyNotFound):
        svc.set_grant(db_conn, int(row[0]), aid, True)


def test_a_key_reads_only_its_granted_accounts(db_conn):
    """Reach: the ACL applies to a key exactly as to any other credential."""
    from localmail.api.acl import allowed_account_ids

    granted = _account(db_conn, "work")
    _account(db_conn, "personal")
    created = svc.create_key(db_conn, name="bot", account_ids=[granted])
    db_conn.commit()
    assert allowed_account_ids(db_conn, created.user_id) == [granted]
