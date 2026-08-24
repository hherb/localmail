# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""JSON routes for /v1/admin/api-keys, driven by an admin bearer token."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.auth import hash_password, issue_token
from localmail.config import ServeConfig
from localmail.serve.app import create_app


@pytest.fixture
def app(db_dsn):
    cfg = ServeConfig(
        session_signing_key="x" * 43, state_signing_key="y" * 43, cookie_secure=False,
    )
    return create_app(db_dsn=db_dsn, serve_config=cfg)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def admin_headers(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('root', %s, TRUE) RETURNING id",
            (hash_password("pw"),),
        )
        row = cur.fetchone()
    assert row is not None
    tok, _ = issue_token(db_conn, int(row[0]))
    db_conn.commit()
    return {"Authorization": f"Bearer {tok}"}


def _account(conn: psycopg.Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port, config) "
            "VALUES (%s, %s, 'password', 'imap.example', 993, '{}'::jsonb) RETURNING id",
            (name, f"{name}@b.test"),
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def test_create_returns_the_key_once(client, db_conn, admin_headers):
    aid = _account(db_conn, "work")
    resp = client.post(
        "/v1/admin/api-keys",
        json={"name": "my_mail_bot", "account_ids": [str(aid)]},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["api_key"].startswith("lmk_")
    assert body["name"] == "my_mail_bot"
    assert isinstance(body["id"], str)

    listed = client.get("/v1/admin/api-keys", headers=admin_headers).json()
    assert "api_key" not in listed["api_keys"][0]
    assert body["api_key"] not in str(listed)


def test_list_reports_grants_and_key_presence(client, db_conn, admin_headers):
    aid = _account(db_conn, "work")
    client.post(
        "/v1/admin/api-keys",
        json={"name": "bot", "account_ids": [str(aid)]},
        headers=admin_headers,
    )
    row = client.get("/v1/admin/api-keys", headers=admin_headers).json()["api_keys"][0]
    assert row["name"] == "bot"
    assert row["has_key"] is True
    assert row["account_names"] == ["work"]


def test_revoke_keeps_the_principal(client, db_conn, admin_headers):
    created = client.post(
        "/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
        headers=admin_headers,
    ).json()
    resp = client.delete(f"/v1/admin/api-keys/{created['id']}", headers=admin_headers)
    assert resp.status_code == 204
    row = client.get("/v1/admin/api-keys", headers=admin_headers).json()["api_keys"][0]
    assert row["has_key"] is False


def test_delete_principal_removes_the_row(client, db_conn, admin_headers):
    created = client.post(
        "/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
        headers=admin_headers,
    ).json()
    resp = client.delete(
        f"/v1/admin/api-keys/{created['id']}/principal", headers=admin_headers
    )
    assert resp.status_code == 204
    assert client.get("/v1/admin/api-keys", headers=admin_headers).json()["api_keys"] == []


def test_a_duplicate_name_is_400(client, db_conn, admin_headers):
    client.post("/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
                headers=admin_headers)
    resp = client.post("/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
                       headers=admin_headers)
    assert resp.status_code == 400


def test_an_unknown_id_is_404(client, db_conn, admin_headers):
    assert client.delete("/v1/admin/api-keys/999999", headers=admin_headers).status_code == 404


def test_a_non_digit_id_is_400(client, db_conn, admin_headers):
    assert client.delete("/v1/admin/api-keys/abc", headers=admin_headers).status_code == 400


def test_grants_can_be_edited(client, db_conn, admin_headers):
    aid = _account(db_conn, "work")
    created = client.post("/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
                          headers=admin_headers).json()
    resp = client.post(
        f"/v1/admin/api-keys/{created['id']}/grants",
        json={"account_id": str(aid), "granted": True},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    row = client.get("/v1/admin/api-keys", headers=admin_headers).json()["api_keys"][0]
    assert row["account_names"] == ["work"]


def test_an_api_key_cannot_mint_another(client, db_conn, admin_headers):
    """Rule 1 end-to-end on the route that matters most.

    The detail string is the assertion, not the 403: this principal is not
    ``is_admin``, so the pre-existing "not an admin" branch answers 403 too, and
    a bare status check passes with Rule 1 deleted outright. Mutation-proven.
    """
    created = client.post("/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
                          headers=admin_headers).json()
    resp = client.post(
        "/v1/admin/api-keys", json={"name": "bot2", "account_ids": []},
        headers={"Authorization": f"Bearer {created['api_key']}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "API keys cannot access admin routes"


def test_a_key_reads_only_its_granted_accounts_over_http(client, db_conn, admin_headers):
    """Reach, end to end: the key drives a real /v1 read through the middleware
    and sees exactly its grants."""
    granted = _account(db_conn, "work")
    _account(db_conn, "personal")
    created = client.post(
        "/v1/admin/api-keys",
        json={"name": "bot", "account_ids": [str(granted)]},
        headers=admin_headers,
    ).json()
    resp = client.get(
        "/v1/accounts", headers={"Authorization": f"Bearer {created['api_key']}"}
    )
    assert resp.status_code == 200
    assert [a["name"] for a in resp.json()] == ["work"]


def test_a_key_with_two_granted_accounts_reports_both(client, db_conn, admin_headers):
    a1 = _account(db_conn, "alpha")
    a2 = _account(db_conn, "beta")
    client.post(
        "/v1/admin/api-keys",
        json={"name": "bot", "account_ids": [str(a1), str(a2)]},
        headers=admin_headers,
    )
    row = client.get("/v1/admin/api-keys", headers=admin_headers).json()["api_keys"][0]
    assert row["account_names"] == ["alpha", "beta"]


def test_key_created_at_and_last_used_at_across_the_lifecycle(client, db_conn, admin_headers):
    created = client.post(
        "/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
        headers=admin_headers,
    ).json()
    row = client.get("/v1/admin/api-keys", headers=admin_headers).json()["api_keys"][0]
    assert row["key_created_at"] is not None
    assert row["last_used_at"] is None

    client.delete(f"/v1/admin/api-keys/{created['id']}", headers=admin_headers)
    row = client.get("/v1/admin/api-keys", headers=admin_headers).json()["api_keys"][0]
    assert row["key_created_at"] is None
    assert row["last_used_at"] is None


# --- CSRF ------------------------------------------------------------------
#
# Every test above authenticates with a *bearer*, and `check_csrf` returns
# immediately for bearer (a bearer header carries no ambient cookie
# credential). So none of them exercises the CSRF branch at all: replacing all
# four `check_csrf` calls in the router with `pass` left the whole file green.
# The cookie path is live — `require_admin` falls back to it — which makes an
# unguarded mint or delete a cross-site surface. These drive it the way
# test_serve_admin_users.py does.


@pytest.fixture
def cookie_admin(app, db_conn):
    import re

    from localmail.api.admin.csrf import make_csrf_token
    from localmail.serve.admin.csrf import csrf_action

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('cookie-root', %s, TRUE) RETURNING id",
            (hash_password("hunter2"),),
        )
        row = cur.fetchone()
    assert row is not None
    admin_id = int(row[0])
    db_conn.commit()

    client = TestClient(app, follow_redirects=False)
    form = client.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m
    resp = client.post("/admin/login", data={
        "username": "cookie-root", "password": "hunter2", "csrf_token": m.group(1)})
    assert resp.status_code == 303, resp.text

    def csrf_for(action: str, method: str = "POST") -> str:
        return make_csrf_token(
            user_id=admin_id, action=csrf_action(method, action),
            key=("x" * 43).encode("ascii"),
        )

    client.csrf_for = csrf_for  # type: ignore[attr-defined]
    return client


def test_minting_a_key_over_a_cookie_session_requires_csrf(cookie_admin):
    resp = cookie_admin.post(
        "/v1/admin/api-keys", json={"name": "bot", "account_ids": []}
    )
    assert resp.status_code == 400
    assert cookie_admin.get("/v1/admin/api-keys").json()["api_keys"] == []


def test_minting_a_key_over_a_cookie_session_succeeds_with_csrf(cookie_admin):
    resp = cookie_admin.post(
        "/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
        headers={"X-CSRF-Token": cookie_admin.csrf_for("/v1/admin/api-keys")},
    )
    assert resp.status_code == 201, resp.text


def test_the_key_csrf_token_is_method_bound(cookie_admin):
    """A POST token must not replay on the DELETE that revokes (#122)."""
    created = cookie_admin.post(
        "/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
        headers={"X-CSRF-Token": cookie_admin.csrf_for("/v1/admin/api-keys")},
    ).json()
    url = f"/v1/admin/api-keys/{created['id']}"
    resp = cookie_admin.request(
        "DELETE", url, headers={"X-CSRF-Token": cookie_admin.csrf_for(url, "POST")}
    )
    assert resp.status_code == 400
    assert cookie_admin.get("/v1/admin/api-keys").json()["api_keys"][0]["has_key"] is True


def test_revoking_a_key_over_a_cookie_session_requires_csrf(cookie_admin):
    created = cookie_admin.post(
        "/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
        headers={"X-CSRF-Token": cookie_admin.csrf_for("/v1/admin/api-keys")},
    ).json()
    resp = cookie_admin.request("DELETE", f"/v1/admin/api-keys/{created['id']}")
    assert resp.status_code == 400
    assert cookie_admin.get("/v1/admin/api-keys").json()["api_keys"][0]["has_key"] is True


def test_deleting_a_principal_over_a_cookie_session_requires_csrf(cookie_admin):
    created = cookie_admin.post(
        "/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
        headers={"X-CSRF-Token": cookie_admin.csrf_for("/v1/admin/api-keys")},
    ).json()
    resp = cookie_admin.request(
        "DELETE", f"/v1/admin/api-keys/{created['id']}/principal"
    )
    assert resp.status_code == 400
    assert len(cookie_admin.get("/v1/admin/api-keys").json()["api_keys"]) == 1


def test_editing_grants_over_a_cookie_session_requires_csrf(cookie_admin, db_conn):
    aid = _account(db_conn, "work")
    created = cookie_admin.post(
        "/v1/admin/api-keys", json={"name": "bot", "account_ids": []},
        headers={"X-CSRF-Token": cookie_admin.csrf_for("/v1/admin/api-keys")},
    ).json()
    resp = cookie_admin.post(
        f"/v1/admin/api-keys/{created['id']}/grants",
        json={"account_id": str(aid), "granted": True},
    )
    assert resp.status_code == 400
    row = cookie_admin.get("/v1/admin/api-keys").json()["api_keys"][0]
    assert row["account_names"] == []
