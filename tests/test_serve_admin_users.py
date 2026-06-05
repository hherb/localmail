"""HTTP-route tests for /v1/admin/users (Sub-plan 2A.4)."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app

_SIGNING_KEY = "x" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_SIGNING_KEY,
        state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
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
            "VALUES ('horst', %s, TRUE) RETURNING id",
            (pwh,),
        )
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


@pytest.fixture
def admin_client(app, admin_user_id):
    import re
    client = TestClient(app, follow_redirects=False)
    form = client.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    r = client.post("/admin/login", data={
        "username": "horst", "password": "hunter2", "csrf_token": m.group(1)})
    assert r.status_code == 303, r.text
    key = _SIGNING_KEY.encode("ascii")

    def csrf_for(action: str, method: str = "POST") -> str:
        return make_csrf_token(
            user_id=admin_user_id, action=csrf_action(method, action), key=key)

    client.csrf_for = csrf_for  # type: ignore[attr-defined]
    return client


def _account(db_conn, name):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            "imap_port, config) VALUES (%s, %s, 'password', 'h', 993, '{}') RETURNING id",
            (name, f"{name}@b.test"))
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


def _make_user(db_conn, username, *, is_admin=False):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES (%s, 'x', %s) RETURNING id", (username, is_admin))
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


def test_list_users_requires_auth(app):
    client = TestClient(app, follow_redirects=False)
    r = client.get("/v1/admin/users")
    assert r.status_code in (302, 303, 401, 403)


def test_list_users_includes_admin(admin_client, admin_user_id):
    r = admin_client.get("/v1/admin/users")
    assert r.status_code == 200, r.text
    users = r.json()["users"]
    assert any(u["username"] == "horst" and u["is_admin"] is True for u in users)
    assert all(isinstance(u["id"], str) for u in users)  # #33 string IDs


def test_create_user(admin_client):
    r = admin_client.post(
        "/v1/admin/users",
        json={"username": "newbie", "password": "pw12345"},
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/users")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["username"] == "newbie"


def test_create_user_requires_csrf(admin_client):
    r = admin_client.post(
        "/v1/admin/users", json={"username": "x", "password": "pw12345"})
    assert r.status_code == 400


def test_create_duplicate_returns_400(admin_client):
    hdr = {"X-CSRF-Token": admin_client.csrf_for("/v1/admin/users")}
    admin_client.post("/v1/admin/users",
                      json={"username": "dup", "password": "pw12345"}, headers=hdr)
    r = admin_client.post("/v1/admin/users",
                          json={"username": "dup", "password": "pw12345"}, headers=hdr)
    assert r.status_code == 400


def test_get_user_detail_has_grants(admin_client, db_conn, admin_user_id):
    _account(db_conn, "alpha")
    r = admin_client.get(f"/v1/admin/users/{admin_user_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(admin_user_id)
    assert any(g["account_name"] == "alpha" for g in body["account_grants"])


def test_patch_demote_self_blocked_409(admin_client, admin_user_id):
    r = admin_client.patch(
        f"/v1/admin/users/{admin_user_id}",
        json={"is_admin": False},
        headers={"X-CSRF-Token": admin_client.csrf_for(f"/v1/admin/users/{admin_user_id}", "PATCH")},
    )
    assert r.status_code == 409, r.text


def test_patch_demote_other_admin_allowed_when_two_exist(admin_client, db_conn, admin_user_id):
    other = _make_user(db_conn, "amy", is_admin=True)
    r = admin_client.patch(
        f"/v1/admin/users/{other}",
        json={"is_admin": False},
        headers={"X-CSRF-Token": admin_client.csrf_for(f"/v1/admin/users/{other}", "PATCH")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_admin"] is False


def test_csrf_token_method_bound(admin_client, db_conn, admin_user_id):
    uid = _make_user(db_conn, "amy")
    patch_token = admin_client.csrf_for(f"/v1/admin/users/{uid}", "PATCH")
    r = admin_client.request(
        "DELETE", f"/v1/admin/users/{uid}", headers={"X-CSRF-Token": patch_token})
    assert r.status_code == 400


def test_delete_self_blocked_409(admin_client, admin_user_id):
    r = admin_client.request(
        "DELETE", f"/v1/admin/users/{admin_user_id}",
        headers={"X-CSRF-Token": admin_client.csrf_for(f"/v1/admin/users/{admin_user_id}", "DELETE")})
    assert r.status_code == 409


def test_grant_round_trip(admin_client, db_conn, admin_user_id):
    uid = _make_user(db_conn, "amy")
    aid = _account(db_conn, "alpha")
    hdr = {"X-CSRF-Token": admin_client.csrf_for(f"/v1/admin/users/{uid}/grants")}
    r = admin_client.post(f"/v1/admin/users/{uid}/grants",
                          json={"account_id": str(aid), "granted": True}, headers=hdr)
    assert r.status_code == 200, r.text
    assert any(g["account_id"] == str(aid) and g["granted"] is True
               for g in r.json()["account_grants"])
