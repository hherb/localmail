"""GET /admin/ — authenticated dashboard placeholder."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.app import create_app


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key="x" * 43,
        state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
    )


@pytest.fixture
def client(db_dsn, serve_cfg):
    return TestClient(create_app(db_dsn=db_dsn, serve_config=serve_cfg), follow_redirects=False)


def test_dashboard_redirects_when_unauthenticated(client: TestClient) -> None:
    r = client.get("/admin/")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/login")


def test_dashboard_renders_when_authenticated(client: TestClient, db_conn: psycopg.Connection) -> None:
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
    client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": csrf},
    )
    r = client.get("/admin/")
    assert r.status_code == 200
    assert "horst" in r.text
    assert "Dashboard" in r.text
