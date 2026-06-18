# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""POST /admin/logout clears the session cookie."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
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
def client(db_dsn, serve_cfg):
    return TestClient(create_app(db_dsn=db_dsn, serve_config=serve_cfg), follow_redirects=False)


def _login(client: TestClient, db_conn: psycopg.Connection) -> None:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, %s, TRUE)",
            ("horst", pwh),
        )
    db_conn.commit()
    import re
    form = client.get("/admin/login").text
    csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', form).group(1)
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": csrf},
    )
    assert r.status_code == 303


def test_logout_clears_cookie(client: TestClient, db_conn: psycopg.Connection, serve_cfg) -> None:
    _login(client, db_conn)
    s_key = serve_cfg.session_signing_key.encode("ascii")
    csrf = make_csrf_token(user_id=1, action="/admin/logout", key=s_key)
    r = client.post("/admin/logout", data={"csrf_token": csrf})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert ("max-age=0" in set_cookie.lower() or "expires=" in set_cookie.lower())
