# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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


def test_set_admin_grant_and_revoke(db_conn):
    keep = _insert_user(db_conn, "keeper", is_admin=True)  # second admin so guard passes
    uid = _insert_user(db_conn, "amy")
    svc.set_admin(db_conn, uid, True)
    assert svc.get_user(db_conn, uid).is_admin is True
    svc.set_admin(db_conn, uid, False)
    assert svc.get_user(db_conn, uid).is_admin is False
    assert svc.get_user(db_conn, keep).is_admin is True


def test_set_admin_revoke_last_admin_blocked(db_conn):
    uid = _insert_user(db_conn, "solo", is_admin=True)
    with pytest.raises(svc.LastAdminError):
        svc.set_admin(db_conn, uid, False)


def test_set_admin_revoke_allowed_with_second_active_admin(db_conn):
    _insert_user(db_conn, "other", is_admin=True)
    uid = _insert_user(db_conn, "amy", is_admin=True)
    svc.set_admin(db_conn, uid, False)  # no raise
    assert svc.get_user(db_conn, uid).is_admin is False


def test_disabled_admin_does_not_count_as_active(db_conn):
    _insert_user(db_conn, "ghost", is_admin=True, disabled=True)  # disabled → not protective
    uid = _insert_user(db_conn, "solo", is_admin=True)
    with pytest.raises(svc.LastAdminError):
        svc.set_admin(db_conn, uid, False)


def test_set_disabled_last_admin_blocked(db_conn):
    uid = _insert_user(db_conn, "solo", is_admin=True)
    with pytest.raises(svc.LastAdminError):
        svc.set_disabled(db_conn, uid, True)


def test_set_disabled_toggle(db_conn):
    _insert_user(db_conn, "other", is_admin=True)
    uid = _insert_user(db_conn, "amy", is_admin=True)
    svc.set_disabled(db_conn, uid, True)
    assert svc.get_user(db_conn, uid).disabled is True
    svc.set_disabled(db_conn, uid, False)
    assert svc.get_user(db_conn, uid).disabled is False


def test_delete_last_admin_blocked(db_conn):
    uid = _insert_user(db_conn, "solo", is_admin=True)
    with pytest.raises(svc.LastAdminError):
        svc.delete_user(db_conn, uid)


def test_delete_user_cascades_grants_and_tokens(db_conn):
    _insert_user(db_conn, "other", is_admin=True)
    uid = _insert_user(db_conn, "amy")
    a1 = _insert_account(db_conn, "alpha")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
            (uid, a1),
        )
    svc.delete_user(db_conn, uid)
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM api_users WHERE id = %s", (uid,))
        assert cur.fetchone() is None
        cur.execute("SELECT 1 FROM user_accounts WHERE user_id = %s", (uid,))
        assert cur.fetchone() is None


def test_delete_unknown_user_raises(db_conn):
    with pytest.raises(UserNotFound):
        svc.delete_user(db_conn, 999999)


def test_set_grant_grant_then_revoke_idempotent(db_conn):
    uid = _insert_user(db_conn, "amy")
    a1 = _insert_account(db_conn, "alpha")
    svc.set_grant(db_conn, uid, a1, True)
    svc.set_grant(db_conn, uid, a1, True)  # idempotent
    assert {g.account_id: g.granted for g in svc.get_user(db_conn, uid).account_grants}[a1] is True
    svc.set_grant(db_conn, uid, a1, False)
    svc.set_grant(db_conn, uid, a1, False)  # idempotent
    assert {g.account_id: g.granted for g in svc.get_user(db_conn, uid).account_grants}[a1] is False


def test_set_grant_unknown_user_raises(db_conn):
    a1 = _insert_account(db_conn, "alpha")
    with pytest.raises(UserNotFound):
        svc.set_grant(db_conn, 999999, a1, True)


def test_revoke_sessions_bumps_timestamp(db_conn):
    uid = _insert_user(db_conn, "amy")
    svc.revoke_sessions(db_conn, uid)
    with db_conn.cursor() as cur:
        cur.execute("SELECT sessions_invalidated_at FROM api_users WHERE id = %s", (uid,))
        row = cur.fetchone()
    assert row is not None and row[0] is not None


def test_revoke_sessions_unknown_user_raises(db_conn):
    with pytest.raises(UserNotFound):
        svc.revoke_sessions(db_conn, 999999)


@pytest.mark.parametrize(
    "is_admin,disabled,count,is_self,is_service,expect",
    [
        (True, False, 1, False, False, {"block_demote": True,  "block_disable": True,  "block_delete": True,  "block_promote": False, "block_password": False}),
        (True, False, 2, False, False, {"block_demote": False, "block_disable": False, "block_delete": False, "block_promote": False, "block_password": False}),
        (True, False, 2, True,  False, {"block_demote": True,  "block_disable": False, "block_delete": True,  "block_promote": False, "block_password": False}),
        (False, False, 5, False, False, {"block_demote": False, "block_disable": False, "block_delete": False, "block_promote": False, "block_password": False}),
        (False, False, 5, True,  False, {"block_demote": True,  "block_disable": False, "block_delete": True,  "block_promote": False, "block_password": False}),
        (False, False, 5, False, True,  {"block_demote": False, "block_disable": False, "block_delete": False, "block_promote": True,  "block_password": True}),
    ],
)
def test_action_flags(is_admin, disabled, count, is_self, is_service, expect):
    flags = svc.action_flags(
        target_is_active_admin=(is_admin and not disabled),
        active_admin_count=count,
        is_self=is_self,
        is_service=is_service,
    )
    assert flags == expect


def test_list_and_get_report_a_service_principal(db_conn):
    """A bot must be distinguishable from a person on the Users screen: the two
    controls it offers — Reset password and Promote — both dead-end at a 400."""
    from localmail.api.admin import api_keys as keys_svc

    _insert_user(db_conn, "amy")
    created = keys_svc.create_key(db_conn, name="my_mail_bot", account_ids=[])
    db_conn.commit()
    by_name = {u.username: u for u in svc.list_users(db_conn)}
    assert by_name["amy"].is_service is False
    assert by_name["my_mail_bot"].is_service is True
    assert svc.get_user(db_conn, created.user_id).is_service is True


def test_a_service_principal_does_not_count_as_an_active_admin(db_conn):
    """A promoted bot must not stand in for a human admin.

    Rule 1 refuses its key at every admin route and Rule 2 refuses its login, so
    a service row carrying is_admin can administer nothing — but while
    active_admin_count still counted it, the last-admin guard let the only human
    be demoted, leaving the archive with no usable administrator.
    """
    from localmail.api.admin import api_keys as keys_svc

    created = keys_svc.create_key(db_conn, name="mail-bot", account_ids=[])
    with db_conn.cursor() as cur:  # the UI refuses to promote it; the DB does not
        cur.execute(
            "UPDATE api_users SET is_admin = TRUE WHERE id = %s", (created.user_id,)
        )
    db_conn.commit()

    assert svc.active_admin_count(db_conn) == 0
    uid = _insert_user(db_conn, "solo", is_admin=True)
    with pytest.raises(svc.LastAdminError):
        svc.set_admin(db_conn, uid, False)


def test_grant_admin_refuses_a_service_principal(db_conn):
    """The CLI's promotion path, guarded like users.set_admin's.

    Contained by Rule 1 either way, but an operator who runs it should be told,
    not left with a bot that /v1/auth/whoami reports as an admin.
    """
    from localmail.api.admin import api_keys as keys_svc
    from localmail.api.admin.auth import grant_admin

    keys_svc.create_key(db_conn, name="mail-bot", account_ids=[])
    db_conn.commit()

    with pytest.raises(svc.UserFieldError, match="API-key principal"):
        grant_admin(db_conn, username="mail-bot")
    with db_conn.cursor() as cur:
        cur.execute("SELECT is_admin FROM api_users WHERE username = 'mail-bot'")
        row = cur.fetchone()
    assert row is not None and row[0] is False
