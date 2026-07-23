# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Bearer-token admin auth on /v1/admin/* (native client path).

A bearer token for an is_admin user drives the admin JSON API with no
CSRF; a non-admin token is 403; a bad token is 401. The cookie web-admin
path still requires and verifies CSRF (regression).
"""
from __future__ import annotations

import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password, issue_token
from localmail.config import ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app

_SIGNING_KEY = "x" * 43


@pytest.fixture
def app(db_dsn):
    cfg = ServeConfig(
        session_signing_key=_SIGNING_KEY, state_signing_key="y" * 43,
        cookie_secure=False,
    )
    return create_app(db_dsn=db_dsn, serve_config=cfg)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


def _make_user(conn: psycopg.Connection, username: str, *, is_admin: bool) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, %s) RETURNING id",
            (username, hash_password("pw"), is_admin),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def admin_token(db_conn):
    uid = _make_user(db_conn, "root", is_admin=True)
    tok, _ = issue_token(db_conn, uid)
    db_conn.commit()
    return tok


@pytest.fixture
def user_token(db_conn):
    uid = _make_user(db_conn, "peon", is_admin=False)
    tok, _ = issue_token(db_conn, uid)
    db_conn.commit()
    return tok


@pytest.fixture
def cookie_client(app, db_conn):
    uid = _make_user(db_conn, "webadmin", is_admin=True)
    db_conn.commit()
    c = TestClient(app, follow_redirects=False)
    form = c.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    r = c.post(
        "/admin/login",
        data={"username": "webadmin", "password": "pw", "csrf_token": m.group(1)},
    )
    assert r.status_code == 303, r.text

    def csrf_for(action: str, method: str = "POST") -> str:
        return make_csrf_token(
            user_id=uid, action=csrf_action(method, action),
            key=_SIGNING_KEY.encode("ascii"),
        )

    c.csrf_for = csrf_for  # type: ignore[attr-defined]
    return c


def _create_body(name: str) -> dict:
    return {"name": name, "email_address": f"{name}@x.test", "auth_method": "archive"}


def test_bearer_admin_lists_accounts(client, admin_token):
    r = client.get("/v1/admin/accounts", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text


def test_bearer_admin_creates_account_without_csrf(client, admin_token):
    r = client.post(
        "/v1/admin/accounts",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=_create_body("work"),
    )
    assert r.status_code in (200, 201), r.text


def test_non_admin_bearer_forbidden(client, user_token):
    r = client.get("/v1/admin/accounts", headers={"Authorization": f"Bearer {user_token}"})
    assert r.status_code == 403


def test_bad_bearer_unauthorized(client):
    r = client.get("/v1/admin/accounts", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_cookie_admin_mutation_without_csrf_still_400(cookie_client):
    r = cookie_client.post("/v1/admin/accounts", json=_create_body("z"))
    assert r.status_code == 400


def test_cookie_admin_mutation_with_csrf_succeeds(cookie_client):
    r = cookie_client.post(
        "/v1/admin/accounts",
        headers={"X-CSRF-Token": cookie_client.csrf_for("/v1/admin/accounts")},
        json=_create_body("z2"),
    )
    assert r.status_code in (200, 201), r.text


@pytest.mark.parametrize("path", ["/v1/admin/users", "/v1/admin/imports", "/v1/admin/daemon"])
def test_bearer_admin_reads_every_admin_router(client, admin_token, path):
    r = client.get(path, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("path", ["/v1/admin/users", "/v1/admin/imports", "/v1/admin/daemon"])
def test_non_admin_bearer_forbidden_on_every_router(client, user_token, path):
    r = client.get(path, headers={"Authorization": f"Bearer {user_token}"})
    assert r.status_code == 403


def test_bearer_admin_creates_user_without_csrf(client, admin_token):
    r = client.post(
        "/v1/admin/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"username": "newbie", "password": "pw"},
    )
    assert r.status_code == 201, r.text


def test_bearer_admin_cancels_unknown_import_without_csrf(client, admin_token):
    r = client.post(
        "/v1/admin/imports/999999/cancel",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 404, r.text


def test_bearer_admin_reloads_daemon_without_csrf(client, admin_token):
    r = client.post(
        "/v1/admin/daemon/reload",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text


def test_bearer_does_not_reach_oauth_start_still_cookie_only(client, admin_token):
    """Phase-1 boundary: oauth_router.oauth_start stays require_admin_session().

    A bearer client hits the cookie path (no cookie -> redirect), so it gets a
    303 to /admin/login, never bearer-authed. This pins the documented Phase-3
    carry-in (OAuth-connect auth story is unresolved) against silent drift: if a
    future change bearer-enables oauth/start, this test flips and forces a
    deliberate update here + in test_session_cookie_scope.py's guard.
    """
    r = client.post(
        "/v1/admin/accounts/1/oauth/start",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 303, r.text
