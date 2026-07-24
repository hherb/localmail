# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""HTML-screen tests for /admin/users (Sub-plan 2A.4)."""
from __future__ import annotations

import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password, verify_password
from localmail.config import ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app

_SIGNING_KEY = "x" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_SIGNING_KEY, state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
        cookie_secure=False)


@pytest.fixture
def app(db_dsn, serve_cfg):
    return create_app(db_dsn=db_dsn, serve_config=serve_cfg)


@pytest.fixture
def admin_user_id(db_conn: psycopg.Connection) -> int:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('horst', %s, TRUE) RETURNING id", (pwh,))
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


@pytest.fixture
def admin_client(app, admin_user_id):
    client = TestClient(app, follow_redirects=False)
    form = client.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    r = client.post("/admin/login", data={
        "username": "horst", "password": "hunter2", "csrf_token": m.group(1)})
    assert r.status_code == 303
    key = _SIGNING_KEY.encode("ascii")

    def csrf_for(action: str, method: str = "POST") -> str:
        return make_csrf_token(
            user_id=admin_user_id, action=csrf_action(method, action), key=key)

    client.csrf_for = csrf_for  # type: ignore[attr-defined]
    return client


def _user(db_conn, username, *, is_admin=False):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, 'x', %s) RETURNING id", (username, is_admin))
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


def test_users_list_requires_auth(app):
    client = TestClient(app, follow_redirects=False)
    r = client.get("/admin/users")
    assert r.status_code in (302, 303)


def test_users_list_renders(admin_client):
    r = admin_client.get("/admin/users")
    assert r.status_code == 200
    assert "horst" in r.text


def test_new_user_form_renders(admin_client):
    r = admin_client.get("/admin/users/new")
    assert r.status_code == 200
    assert 'name="username"' in r.text


def test_create_blank_username_inline_error(admin_client):
    r = admin_client.post(
        "/admin/users", data={"username": "", "password": "pw12345"},
        headers={"X-CSRF-Token": admin_client.csrf_for("/admin/users")})
    assert r.status_code == 400
    assert "username" in r.text.lower()
    # The users form relies on the same admin-forms.js swap mechanism as the
    # accounts form: the 400 must be an HTML fragment whose root id matches the
    # form's hx-target (#user-form-fields) or htmx drops it silently.
    assert r.headers["content-type"].startswith("text/html")
    assert 'id="user-form-fields"' in r.text


def test_admin_pages_load_error_surfacing_script(admin_client):
    # base.html must load admin-forms.js on the users pages too; without it the
    # 400 validation fragment above is dropped and a rejected create looks inert.
    page = admin_client.get("/admin/users/new").text
    assert "/admin/static/admin-forms.js" in page


def test_create_success_redirects(admin_client):
    r = admin_client.post(
        "/admin/users", data={"username": "newbie", "password": "pw12345"},
        headers={"X-CSRF-Token": admin_client.csrf_for("/admin/users")})
    assert r.status_code == 200
    assert r.headers["HX-Redirect"].startswith("/admin/users/")


def test_edit_screen_disables_self_demote(admin_client, admin_user_id):
    r = admin_client.get(f"/admin/users/{admin_user_id}")
    assert r.status_code == 200
    assert "disabled" in r.text


def test_grant_toggle_swaps_grants_fragment(admin_client, db_conn, admin_user_id):
    uid = _user(db_conn, "amy")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            "imap_port, config) VALUES ('alpha', 'a@b.test', 'password', 'h', 993, '{}') "
            "RETURNING id")
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    aid = int(row[0])
    r = admin_client.post(
        f"/admin/users/{uid}/grants",
        data={"account_id": str(aid), "granted": "true"},
        headers={"X-CSRF-Token": admin_client.csrf_for(f"/admin/users/{uid}/grants")})
    assert r.status_code == 200
    assert "alpha" in r.text


def test_delete_self_blocked_fragment(admin_client, admin_user_id):
    r = admin_client.post(
        f"/admin/users/{admin_user_id}/delete",
        headers={"X-CSRF-Token": admin_client.csrf_for(f"/admin/users/{admin_user_id}/delete")})
    assert r.status_code == 409
    assert "your own" in r.text.lower()


def test_edit_password_input_is_htmx_includable(admin_client, admin_user_id):
    """Regression (#160): the reset-password input must not carry a `form`
    attribute at all. Any `form` value binds it to that form (here a
    non-existent one), so htmx's hx-include serialises an empty password ->
    the endpoint 400s -> htmx does not swap 4xx -> the change silently no-ops.
    The working accounts form has no `form` attribute; the users form must not
    either.

    Proxy test: TestClient runs no JS, so this asserts the attribute is gone
    rather than observing the browser's htmx serialisation directly. A true
    end-to-end guard would need a browser (Playwright) test."""
    r = admin_client.get(f"/admin/users/{admin_user_id}")
    assert r.status_code == 200
    m = re.search(r'<input[^>]*name="password"[^>]*>', r.text)
    assert m, "reset-password input not found on the edit screen"
    tag = m.group(0)
    assert "form=" not in tag, tag


def test_set_password_persists(admin_client, db_conn, admin_user_id):
    """When the password field is actually sent, the endpoint returns 200 and
    the new hash is persisted (guards the server contract the template feeds)."""
    r = admin_client.post(
        f"/admin/users/{admin_user_id}/password",
        data={"password": "brand-new-secret-9"},
        headers={"X-CSRF-Token": admin_client.csrf_for(
            f"/admin/users/{admin_user_id}/password")})
    assert r.status_code == 200
    assert "Password updated" in r.text
    with db_conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM api_users WHERE id = %s",
                    (admin_user_id,))
        row = cur.fetchone()
    assert row is not None
    assert verify_password("brand-new-secret-9", row[0])
