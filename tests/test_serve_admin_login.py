"""GET/POST /admin/login: render form, validate creds, issue cookie."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.admin.dependencies import SESSION_COOKIE_NAME
from localmail.serve.app import create_app


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key="x" * 43,
        state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
        cookie_secure=False,  # TestClient uses http://testserver
    )


@pytest.fixture
def app(db_dsn, serve_cfg):
    return create_app(db_dsn=db_dsn, serve_config=serve_cfg)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def admin_user(db_conn: psycopg.Connection) -> int:
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


def test_get_login_renders_form(client: TestClient) -> None:
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert "<form" in r.text and 'name="username"' in r.text and 'name="password"' in r.text
    assert 'name="csrf_token"' in r.text


def test_post_login_success_issues_cookie(client: TestClient, admin_user: int) -> None:
    form = client.get("/admin/login").text
    csrf = _extract_csrf(form)
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": csrf},
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/admin/"
    cookie = r.cookies.get(SESSION_COOKIE_NAME)
    assert cookie is not None


def test_post_login_wrong_password_re_renders_form(client: TestClient, admin_user: int) -> None:
    form = client.get("/admin/login").text
    csrf = _extract_csrf(form)
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "wrong", "csrf_token": csrf},
    )
    assert r.status_code == 401
    assert "invalid credentials" in r.text.lower()


def test_post_login_non_admin_rejected(
    client: TestClient, db_conn: psycopg.Connection, caplog
) -> None:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, FALSE)",
            ("regular", pwh),
        )
    db_conn.commit()
    form = client.get("/admin/login").text
    csrf = _extract_csrf(form)
    import logging
    with caplog.at_level(logging.WARNING, logger="localmail.serve.admin"):
        r = client.post(
            "/admin/login",
            data={"username": "regular", "password": "hunter2", "csrf_token": csrf},
        )
    # Wire response collapses to 401 with a generic message — the response
    # must not betray "this user exists with this password but isn't an admin".
    assert r.status_code == 401
    assert "invalid credentials" in r.text.lower()
    assert "not an admin" not in r.text.lower()
    # The server-side log keeps the distinction so operators can see when a
    # legitimate non-admin user mistypes the admin URL.
    assert any(
        "non-admin login attempt" in rec.message and "regular" in rec.message
        for rec in caplog.records
    )


def test_post_login_missing_csrf_rejected(client: TestClient, admin_user: int) -> None:
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2"},  # no csrf_token
    )
    assert r.status_code == 400


def test_post_login_bad_csrf_rejected(client: TestClient, admin_user: int) -> None:
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": "not-a-real-token"},
    )
    assert r.status_code == 400


def _extract_csrf(html: str) -> str:
    import re
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, f"no csrf_token in form html"
    return m.group(1)


def test_cookie_secure_attr_driven_by_config(db_dsn, db_conn: psycopg.Connection) -> None:
    """cookie_secure=True sets Secure on the cookie even when the inbound
    request looks like plain HTTP — that's the production case behind a
    TLS-terminating reverse proxy where uvicorn sees http but the wire is https.
    """
    cfg = ServeConfig(
        session_signing_key="x" * 43,
        state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
        cookie_secure=True,
    )
    app = create_app(db_dsn=db_dsn, serve_config=cfg)
    client = TestClient(app, follow_redirects=False)

    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, TRUE)",
            ("horst", pwh),
        )
    db_conn.commit()

    form = client.get("/admin/login").text
    csrf = _extract_csrf(form)
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": csrf},
    )
    assert r.status_code == 303, r.text
    set_cookie = r.headers["set-cookie"]
    # http://testserver but cookie_secure=True → Secure attribute set anyway.
    assert "Secure" in set_cookie, set_cookie
