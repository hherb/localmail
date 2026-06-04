"""Admin account-management HTML screens (2A.3)."""
from __future__ import annotations

import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin import accounts as svc
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.app import create_app

_SIGNING_KEY = "x" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_SIGNING_KEY,
        state_signing_key="y" * 43,
        cookie_secure=False,
    )


@pytest.fixture
def app(db_dsn, serve_cfg):
    return create_app(db_dsn=db_dsn, serve_config=serve_cfg)


@pytest.fixture
def admin_user_id(db_conn: psycopg.Connection) -> int:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, TRUE) RETURNING id",
            ("horst", pwh),
        )
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
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": m.group(1)},
    )
    assert r.status_code == 303, r.text
    client.app_state_admin_id = admin_user_id
    return client


def _seed_account(db_conn, **over) -> int:
    kw = dict(
        name="fastmail", email_address="me@fastmail.com", auth_method="password",
        imap_host="imap.fastmail.com", imap_port=993, oauth_provider=None,
        folder_allow=None, folder_deny=None, folder_deny_flags=None,
    )
    kw.update(over)
    acct = svc.create_account(db_conn, **kw)
    db_conn.commit()
    return acct.id


def test_list_redirects_unauthenticated(app):
    client = TestClient(app, follow_redirects=False)
    r = client.get("/admin/accounts")
    assert r.status_code in (302, 303)
    assert "/admin/login" in r.headers["location"]


def test_list_renders_accounts(admin_client, db_conn):
    _seed_account(db_conn, name="fastmail")
    _seed_account(db_conn, name="work-gmail", email_address="me@company.com",
                  auth_method="oauth2", imap_host="imap.gmail.com",
                  oauth_provider="gmail")
    r = admin_client.get("/admin/accounts")
    assert r.status_code == 200
    assert "fastmail" in r.text
    assert "work-gmail" in r.text
    assert "New account" in r.text


def _csrf_header(client, path: str) -> dict:
    """Mint a method-bound POST token by scraping it from a rendered form/page.

    The new-account form embeds the create token in a hx-headers attribute on
    the <form>; we read it from there so the test exercises the real mint.
    """
    page = client.get("/admin/accounts/new").text
    m = re.search(r'data-create-csrf="([^"]+)"', page)
    assert m, "create CSRF token not found in new-account form"
    return {"X-CSRF-Token": m.group(1)}


def test_new_account_form_renders(admin_client):
    r = admin_client.get("/admin/accounts/new")
    assert r.status_code == 200
    assert 'name="auth_method"' in r.text
    assert "/admin/static/accounts-panel.js" in r.text


def test_create_account_happy_path(admin_client, db_conn):
    headers = _csrf_header(admin_client, "/admin/accounts")
    r = admin_client.post(
        "/admin/accounts",
        data={
            "name": "fastmail", "email_address": "me@fastmail.com",
            "auth_method": "password", "imap_host": "imap.fastmail.com",
            "imap_port": "993", "oauth_provider": "",
            "folder_allow": "INBOX", "folder_deny": "",
        },
        headers=headers,
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect", "").startswith("/admin/accounts/")
    with db_conn.cursor() as cur:
        cur.execute("SELECT name FROM accounts WHERE name = 'fastmail'")
        assert cur.fetchone() is not None


def test_create_account_validation_error_inline(admin_client):
    headers = _csrf_header(admin_client, "/admin/accounts")
    r = admin_client.post(
        "/admin/accounts",
        data={
            "name": "x", "email_address": "x@x.com", "auth_method": "password",
            "imap_host": "h", "imap_port": "70000", "oauth_provider": "",
            "folder_allow": "", "folder_deny": "",
        },
        headers=headers,
    )
    assert r.status_code == 400
    assert "imap_port" in r.text


def test_create_account_missing_csrf_rejected(admin_client):
    r = admin_client.post(
        "/admin/accounts",
        data={"name": "y", "email_address": "y@y.com", "auth_method": "archive"},
    )
    assert r.status_code == 400
