"""require_admin_session dependency: cookie → AdminUser, else redirect/403."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from localmail.api.admin.auth import AdminUser
from localmail.api.admin.session_tokens import SessionPayload, encode_session_token
from localmail.api.auth import hash_password
from localmail.serve.admin.dependencies import (
    SESSION_COOKIE_NAME,
    install_admin_redirect_handler,
    require_admin_session,
)

KEY = b"a" * 32


def _make_app(pool: ConnectionPool, *, key: bytes = KEY) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool
    app.state.serve_config = type("Cfg", (), {
        "session_signing_key": key.decode("ascii"),
    })()

    @app.get("/admin/probe")
    def probe(user: AdminUser = require_admin_session()):  # type: ignore[assignment]
        return {"id": user.id, "username": user.username}

    install_admin_redirect_handler(app)
    return app


@pytest.fixture
def pool(db_conn, db_dsn):
    # db_conn's fixture body truncates all data tables before each test
    p = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    yield p
    p.close()


def _seed_admin(pool: ConnectionPool, username: str = "horst") -> int:
    pwh = hash_password("hunter2")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_users (username, password_hash, is_admin) "
                "VALUES (%s, %s, TRUE) RETURNING id",
                (username, pwh),
            )
            row = cur.fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def test_no_cookie_redirects(pool: ConnectionPool) -> None:
    client = TestClient(_make_app(pool), follow_redirects=False)
    r = client.get("/admin/probe")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/login")


def test_valid_cookie_admits(pool: ConnectionPool) -> None:
    uid = _seed_admin(pool)
    now = int(time.time())
    tok = encode_session_token(
        SessionPayload(user_id=uid, issued_at=now, exp=now + 3600),
        key=KEY,
    )
    client = TestClient(_make_app(pool), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, tok)
    r = client.get("/admin/probe")
    assert r.status_code == 200
    assert r.json() == {"id": uid, "username": "horst"}


def test_tampered_cookie_redirects(pool: ConnectionPool) -> None:
    uid = _seed_admin(pool)
    now = int(time.time())
    tok = encode_session_token(
        SessionPayload(user_id=uid, issued_at=now, exp=now + 3600),
        key=KEY,
    )
    body, sig = tok.split(".")
    tampered = body + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B")
    client = TestClient(_make_app(pool), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, tampered)
    r = client.get("/admin/probe")
    assert r.status_code == 303


def test_expired_cookie_redirects(pool: ConnectionPool) -> None:
    uid = _seed_admin(pool)
    now = int(time.time())
    tok = encode_session_token(
        SessionPayload(user_id=uid, issued_at=now - 10, exp=now - 1),
        key=KEY,
    )
    client = TestClient(_make_app(pool), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, tok)
    r = client.get("/admin/probe")
    assert r.status_code == 303


def test_non_admin_user_403(pool: ConnectionPool) -> None:
    uid = _seed_admin(pool, "regular")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE api_users SET is_admin = FALSE WHERE id = %s", (uid,))
        conn.commit()
    now = int(time.time())
    tok = encode_session_token(
        SessionPayload(user_id=uid, issued_at=now, exp=now + 3600),
        key=KEY,
    )
    client = TestClient(_make_app(pool), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, tok)
    r = client.get("/admin/probe")
    assert r.status_code == 403


def test_user_deleted_after_cookie_issued_redirects(pool: ConnectionPool) -> None:
    uid = _seed_admin(pool)
    now = int(time.time())
    tok = encode_session_token(
        SessionPayload(user_id=uid, issued_at=now, exp=now + 3600),
        key=KEY,
    )
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM api_users WHERE id = %s", (uid,))
        conn.commit()
    client = TestClient(_make_app(pool), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, tok)
    r = client.get("/admin/probe")
    assert r.status_code == 303
