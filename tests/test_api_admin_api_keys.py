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


def test_the_raw_key_is_stored_in_no_column_of_any_row(db_conn):
    """Secrecy: the spec's requirement is every column, not the hash column —
    and `raw_key in token_sha256` cannot be true either way, a 47-byte needle
    in a 32-byte haystack."""
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    needle = created.raw_key
    with db_conn.cursor() as cur:
        cur.execute("SELECT * FROM api_tokens")
        rows = cur.fetchall()
        columns = [d.name for d in cur.description or []]
    assert rows, "expected the minted key's row"
    assert "token_sha256" in columns
    for row in rows:
        for column, value in zip(columns, row):
            rendered = value.decode("utf-8", "replace") if isinstance(
                value, (bytes, bytearray, memoryview)
            ) else str(value)
            assert needle not in rendered, column
    assert [r[columns.index("api_key_name")] for r in rows] == ["bot"]


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


def test_the_raw_key_is_not_in_the_repr(db_conn):
    """One `logging.info("%s", created)` must not leak the credential."""
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    assert created.raw_key not in repr(created)
    assert "bot" in repr(created)


def test_a_stale_no_key_verdict_is_a_field_error_not_a_crash(db_conn, monkeypatch):
    """The check-then-INSERT race, reproduced by forcing the verdict its loser
    holds: _resolve_principal reported the name free, and the INSERT then meets
    the partial unique index. Uncaught that bypassed the routers'
    ApiKeyFieldError -> 400 contract and surfaced as a 500."""
    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    monkeypatch.setattr(
        svc, "_resolve_principal", lambda conn, name: created.user_id
    )
    with pytest.raises(svc.ApiKeyFieldError, match="already exists"):
        svc.create_key(db_conn, name="bot", account_ids=[])


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_a_blank_name_is_refused_at_the_service_layer(db_conn, name):
    """The JSON route declares `name: str` with no min_length and the CLI passes
    NAME straight through, so this line is the only thing between either and an
    unnamed principal. Deleting it left all 44 API-key tests green."""
    with pytest.raises(svc.ApiKeyFieldError, match="blank"):
        svc.create_key(db_conn, name=name, account_ids=[])


def test_an_overlong_name_is_refused(db_conn):
    with pytest.raises(svc.ApiKeyFieldError, match="longer than"):
        svc.create_key(db_conn, name="b" * 129, account_ids=[])


def test_the_stored_name_is_stripped(db_conn):
    """The stripped value feeds both api_users.username and api_tokens
    .api_key_name, so a surrounding space would make the CLI's name-based
    lookups miss the row they just created."""
    created = svc.create_key(db_conn, name="  bot  ", account_ids=[])
    assert created.name == "bot"
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT u.username, t.api_key_name FROM api_users u "
            "JOIN api_tokens t ON t.user_id = u.id WHERE u.id = %s",
            (created.user_id,),
        )
        row = cur.fetchone()
    assert row == ("bot", "bot")


def test_reported_state_matches_whether_the_key_verifies(db_conn):
    """Differential pin: `has_key and not disabled and not revoked` must agree
    with `verify_token` across every state an operator can put a key in.

    The two causes are a hand-maintained restatement of credential_valid_sql's
    halves -- the ALLOWLISTED_WHERE_SQL arrangement -- so this is what keeps
    them from drifting. Before it, the panel said "active" while the bot got a
    bare 401 with nothing anywhere to diagnose it.
    """
    from localmail.api.admin import users as users_svc
    from localmail.api.auth import verify_token

    def reported_live() -> bool:
        row = svc.list_keys(db_conn)[0]
        return row.has_key and not row.disabled and not row.revoked

    created = svc.create_key(db_conn, name="bot", account_ids=[])
    db_conn.commit()
    assert reported_live() is True
    assert verify_token(db_conn, created.raw_key) is not None

    users_svc.set_disabled(db_conn, created.user_id, True)
    db_conn.commit()
    assert reported_live() is False
    assert verify_token(db_conn, created.raw_key) is None

    users_svc.set_disabled(db_conn, created.user_id, False)
    db_conn.commit()
    assert reported_live() is True
    assert verify_token(db_conn, created.raw_key) is not None

    users_svc.revoke_sessions(db_conn, created.user_id)
    db_conn.commit()
    assert reported_live() is False
    assert verify_token(db_conn, created.raw_key) is None

    svc.revoke_key(db_conn, created.user_id)
    db_conn.commit()
    assert reported_live() is False
    assert verify_token(db_conn, created.raw_key) is None
