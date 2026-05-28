"""Admin login participates in api_login_attempts rate limiting."""
from __future__ import annotations
import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.auth import hash_password
from localmail.config import AuthConfig, ServeConfig
from localmail.serve.app import create_app


def _csrf(html: str) -> str:
    import re
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m is not None, "No csrf_token found in HTML"
    return m.group(1)


@pytest.fixture
def app(db_dsn):
    cfg = ServeConfig(session_signing_key="x"*43, state_signing_key="y"*43,
                      oauth_callback_url="https://example.com/admin/oauth/callback")
    # Tighten the per-user cap to make the test fast.
    auth_cfg = AuthConfig(login_per_user_max=2, login_per_user_window_s=60)
    return create_app(db_dsn=db_dsn, serve_config=cfg, auth_config=auth_cfg)


def test_failed_admin_logins_record_in_api_login_attempts(app, db_conn: psycopg.Connection) -> None:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) VALUES (%s, %s, TRUE)",
            ("horst", pwh),
        )
    db_conn.commit()
    client = TestClient(app, follow_redirects=False)
    form = client.get("/admin/login").text
    csrf = _csrf(form)
    r = client.post("/admin/login", data={"username": "horst", "password": "wrong", "csrf_token": csrf})
    assert r.status_code == 401
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM api_login_attempts WHERE username = 'horst' AND outcome = 'failure'")
        n = cur.fetchone()[0]
    assert n >= 1


def test_per_user_rate_limit_triggers_429(app, db_conn: psycopg.Connection) -> None:
    pwh = hash_password("hunter2")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) VALUES (%s, %s, TRUE)",
            ("horst", pwh),
        )
    db_conn.commit()
    client = TestClient(app, follow_redirects=False)
    form = client.get("/admin/login").text
    csrf = _csrf(form)
    for _ in range(3):  # exceeds per_user_max=2
        client.post("/admin/login", data={"username": "horst", "password": "wrong", "csrf_token": csrf})
    # Next attempt should be rate-limited even with correct password
    form = client.get("/admin/login").text
    csrf = _csrf(form)
    r = client.post("/admin/login", data={"username": "horst", "password": "hunter2", "csrf_token": csrf})
    assert r.status_code == 429
