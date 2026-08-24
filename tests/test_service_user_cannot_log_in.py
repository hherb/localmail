# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A service user is a machine principal; no password path may admit it.

One test per verifying site, deliberately not parametrised over the shared
fragment: the drift the fragment exists to prevent is a site that does not use
it, which a test calling the fragment directly cannot see.
"""
from __future__ import annotations

import psycopg
import pytest

from localmail.api import auth as api_auth
from localmail.api.admin import auth as admin_auth
from localmail.api.admin import users as users_svc
from localmail.api.errors import AuthenticationFailed
from localmail.api.login_eligible_sql import login_eligible_sql

_PW = "correct-horse"


def _service_user(conn: psycopg.Connection, username: str = "bot") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_service, is_admin) "
            "VALUES (%s, %s, TRUE, TRUE) RETURNING id",
            (username, api_auth.hash_password(_PW)),
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_fragment_is_parenthesised_and_names_its_alias():
    sql = login_eligible_sql(user="u")
    assert sql.startswith("(") and sql.endswith(")")
    assert "u.disabled_at IS NULL" in sql
    assert "u.is_service IS FALSE" in sql


def test_v1_auth_login_refuses_a_service_user(db_conn):
    _service_user(db_conn)
    with pytest.raises(AuthenticationFailed):
        api_auth.login(db_conn, "bot", _PW)


def test_admin_cookie_login_refuses_a_service_user(db_conn):
    _service_user(db_conn)
    with pytest.raises(AuthenticationFailed):
        admin_auth.authenticate_admin(db_conn, username="bot", password=_PW)


def test_oauth_consent_login_refuses_a_service_user(db_conn):
    """The consent router verifies its own password lookup inline."""
    _service_user(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM api_users WHERE username = %s AND "
            + login_eligible_sql(user="api_users"),
            ("bot",),
        )
        assert cur.fetchone() is None

    # An AST check plus a negative control, not a source-text match (#291) and
    # not a bare import check: together they prove the module calls the fragment
    # AND no longer carries the wording the fragment replaced. Driving the real
    # consent route needs a full PKCE client, so this pins the seam instead.
    import ast
    import inspect

    from localmail.serve.oauth import consent_router

    tree = ast.parse(inspect.getsource(consent_router))
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "login_eligible_sql" in calls

    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any("disabled_at IS NULL" in lit for lit in literals), (
        "consent_router still carries the wording login_eligible_sql replaced"
    )


def test_a_human_still_logs_in(db_conn):
    """Positive control: the fragment must not lock everyone out."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES (%s, %s)",
            ("human", api_auth.hash_password(_PW)),
        )
    db_conn.commit()
    token, _ = api_auth.login(db_conn, "human", _PW)
    assert token


def test_password_reset_is_refused_on_a_service_row(db_conn):
    """Without this the Users panel hands a bot an interactive login — the one
    path that makes the unusable password hash usable again."""
    uid = _service_user(db_conn)
    with pytest.raises(users_svc.UserFieldError):
        users_svc.set_password(db_conn, uid, "new-password")


def test_admin_promotion_is_refused_on_a_service_row(db_conn):
    uid = _service_user(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_users SET is_admin = FALSE WHERE id = %s", (uid,))
    db_conn.commit()
    with pytest.raises(users_svc.UserFieldError):
        users_svc.set_admin(db_conn, uid, True)


def test_change_password_refuses_a_service_user(db_conn):
    """The fourth site. A service principal that somehow learned its random
    password must still not be able to set a new one — that would hand it the
    interactive login Rule 2 exists to deny."""
    uid = _service_user(db_conn)
    with pytest.raises(AuthenticationFailed):
        api_auth.change_password(
            db_conn, uid, old_password=_PW, new_password="new-password"
        )
