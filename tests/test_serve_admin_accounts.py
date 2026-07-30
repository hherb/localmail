# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""HTTP-route tests for /v1/admin/accounts (Sub-plan 2A)."""
from __future__ import annotations

from contextlib import contextmanager

import keyring
import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app
from tests._fake_imap import FakeIMAPClient


_SIGNING_KEY = "x" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_SIGNING_KEY,
        state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
        cookie_secure=False,  # TestClient uses http://testserver
    )


@pytest.fixture
def app(db_dsn, serve_cfg):
    return create_app(db_dsn=db_dsn, serve_config=serve_cfg)


@pytest.fixture
def client_no_auth(app):
    """Unauthenticated TestClient — no session cookie installed."""
    return TestClient(app, follow_redirects=False)


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
    """TestClient authenticated via the real /admin/login flow.

    Returns a callable ``csrf_for(action)`` plus the client. We attach the
    CSRF helper directly to the client to keep the test signatures compact.
    """
    import re

    client = TestClient(app, follow_redirects=False)
    form = client.get("/admin/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', form)
    assert m, "no csrf_token in login form"
    csrf = m.group(1)
    r = client.post(
        "/admin/login",
        data={"username": "horst", "password": "hunter2", "csrf_token": csrf},
    )
    assert r.status_code == 303, r.text

    key = _SIGNING_KEY.encode("ascii")

    def csrf_for(action: str, method: str = "POST") -> str:
        bound = csrf_action(method, action)
        return make_csrf_token(user_id=admin_user_id, action=bound, key=key)

    client.csrf_for = csrf_for  # type: ignore[attr-defined]
    return client


# ---------- list ----------

def test_list_accounts_returns_empty_when_none(admin_client):
    r = admin_client.get("/v1/admin/accounts")
    assert r.status_code == 200, r.text
    assert r.json() == {"accounts": []}


def test_list_accounts_returns_summaries(admin_client, db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port, config) "
            "VALUES (%s, %s, %s, %s, %s, '{}'::jsonb)",
            ("alpha", "a@b.test", "password", "imap.example", 993),
        )
    db_conn.commit()
    r = admin_client.get("/v1/admin/accounts")
    assert r.status_code == 200
    data = r.json()
    assert len(data["accounts"]) == 1
    assert data["accounts"][0]["name"] == "alpha"
    # IDs are emitted as strings on the wire (#33 convention).
    assert isinstance(data["accounts"][0]["id"], str)


# ---------- create ----------

def test_create_account_password_round_trip(admin_client):
    body = {
        "name": "created-via-api",
        "email_address": "x@y.test",
        "auth_method": "password",
        "imap_host": "imap.example",
        "imap_port": 993,
    }
    r = admin_client.post(
        "/v1/admin/accounts",
        json=body,
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/accounts")},
    )
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["name"] == "created-via-api"
    assert j["auth_method"] == "password"
    assert isinstance(j["id"], str)

    r2 = admin_client.get(f"/v1/admin/accounts/{j['id']}")
    assert r2.status_code == 200
    assert r2.json()["id"] == j["id"]


def test_create_account_validation_error_is_400(admin_client):
    r = admin_client.post(
        "/v1/admin/accounts",
        json={
            "name": "",
            "email_address": "x@y.test",
            "auth_method": "password",
            "imap_host": "h",
            "imap_port": 993,
        },
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/accounts")},
    )
    assert r.status_code == 400


def test_create_account_without_csrf_is_400(admin_client):
    r = admin_client.post(
        "/v1/admin/accounts",
        json={
            "name": "x",
            "email_address": "x@y.test",
            "auth_method": "password",
            "imap_host": "h",
            "imap_port": 993,
        },
    )
    assert r.status_code == 400


# ---------- get ----------

def test_get_account_404(admin_client):
    r = admin_client.get("/v1/admin/accounts/9999")
    assert r.status_code == 404


def test_get_account_non_digit_id_400(admin_client):
    r = admin_client.get("/v1/admin/accounts/not-a-number")
    assert r.status_code == 400


# ---------- patch ----------

def test_patch_account_changes_folder_deny(admin_client):
    create = admin_client.post(
        "/v1/admin/accounts",
        json={
            "name": "patchable",
            "email_address": "a@b.test",
            "auth_method": "password",
            "imap_host": "h",
            "imap_port": 993,
        },
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/accounts")},
    )
    assert create.status_code == 201, create.text
    aid = create.json()["id"]
    r = admin_client.patch(
        f"/v1/admin/accounts/{aid}",
        json={"folder_deny": ["Spam", "Trash"]},
        headers={
            "X-CSRF-Token": admin_client.csrf_for(
                f"/v1/admin/accounts/{aid}", "PATCH"
            ),
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["folder_deny"] == ["Spam", "Trash"]


def test_patch_csrf_token_is_not_replayable_on_delete(admin_client):
    """A CSRF token minted for PATCH must not authorize DELETE on the same
    shared path — CSRF actions are method-bound (#122)."""
    create = admin_client.post(
        "/v1/admin/accounts",
        json={
            "name": "method-bound",
            "email_address": "x@y.test",
            "auth_method": "password",
            "imap_host": "h",
            "imap_port": 993,
        },
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/accounts")},
    )
    assert create.status_code == 201, create.text
    aid = create.json()["id"]
    # Token minted for PATCH, replayed against DELETE on the same path.
    patch_token = admin_client.csrf_for(f"/v1/admin/accounts/{aid}", "PATCH")
    r = admin_client.delete(
        f"/v1/admin/accounts/{aid}",
        headers={"X-CSRF-Token": patch_token},
    )
    assert r.status_code == 400, r.text


# ---------- delete ----------

def test_delete_empty_account_returns_204(admin_client):
    create = admin_client.post(
        "/v1/admin/accounts",
        json={
            "name": "deletable",
            "email_address": "x@y.test",
            "auth_method": "password",
            "imap_host": "h",
            "imap_port": 993,
        },
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/accounts")},
    )
    assert create.status_code == 201, create.text
    aid = create.json()["id"]
    r = admin_client.delete(
        f"/v1/admin/accounts/{aid}",
        headers={
            "X-CSRF-Token": admin_client.csrf_for(
                f"/v1/admin/accounts/{aid}", "DELETE"
            ),
        },
    )
    assert r.status_code == 204


def test_delete_account_with_messages_returns_409(admin_client, db_conn):
    create = admin_client.post(
        "/v1/admin/accounts",
        json={
            "name": "busy",
            "email_address": "x@y.test",
            "auth_method": "password",
            "imap_host": "h",
            "imap_port": 993,
        },
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/accounts")},
    )
    aid = create.json()["id"]
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (account_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments) "
            "VALUES (%s, %s, %s, %s, '{}'::jsonb, '[]'::jsonb)",
            (int(aid), b"x", b"a" * 32, 1),
        )
    db_conn.commit()
    r = admin_client.delete(
        f"/v1/admin/accounts/{aid}",
        headers={
            "X-CSRF-Token": admin_client.csrf_for(
                f"/v1/admin/accounts/{aid}", "DELETE"
            ),
        },
    )
    assert r.status_code == 409


# ---------- password ----------

def test_post_password_stores_in_keyring(admin_client):
    create = admin_client.post(
        "/v1/admin/accounts",
        json={
            "name": "pw-target",
            "email_address": "x@y.test",
            "auth_method": "password",
            "imap_host": "h",
            "imap_port": 993,
        },
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/accounts")},
    )
    aid = create.json()["id"]
    r = admin_client.post(
        f"/v1/admin/accounts/{aid}/password",
        json={"password": "sekret"},
        headers={
            "X-CSRF-Token": admin_client.csrf_for(
                f"/v1/admin/accounts/{aid}/password"
            ),
        },
    )
    assert r.status_code == 204, r.text
    assert keyring.get_password("localmail", "pw-target") == "sekret"


# ---------- test-connection ----------

def test_test_connection_returns_folders(admin_client, monkeypatch):
    create = admin_client.post(
        "/v1/admin/accounts",
        json={
            "name": "tc",
            "email_address": "x@y.test",
            "auth_method": "password",
            "imap_host": "imap.example",
            "imap_port": 993,
        },
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/accounts")},
    )
    aid = create.json()["id"]

    fake = FakeIMAPClient.with_folders(["INBOX", "Sent", "[Gmail]/All Mail"])

    @contextmanager
    def fake_open_connection(account, *, gmail_client_secrets=None):
        yield fake

    monkeypatch.setattr(
        "localmail.api.admin.accounts._open_imap_connection",
        fake_open_connection,
    )
    r = admin_client.post(
        f"/v1/admin/accounts/{aid}/test-connection",
        headers={
            "X-CSRF-Token": admin_client.csrf_for(
                f"/v1/admin/accounts/{aid}/test-connection"
            ),
        },
    )
    assert r.status_code == 200, r.text
    names = [f["name"] for f in r.json()["folders"]]
    assert names == ["INBOX", "Sent", "[Gmail]/All Mail"]


def test_test_connection_oauth2_returns_400_not_500(admin_client):
    """Gmail OAuth account at /test-connection: until Sub-plan 2A.2 wires
    the Google client secrets through `_open_imap_connection`, the
    service layer must short-circuit with AccountFieldError so the route
    translates to a clean 400 rather than a 500 from imap_client."""
    create = admin_client.post(
        "/v1/admin/accounts",
        json={
            "name": "tc-oauth",
            "email_address": "g@example.test",
            "auth_method": "oauth2",
            "oauth_provider": "gmail",
            "imap_host": "imap.gmail.com",
            "imap_port": 993,
        },
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/accounts")},
    )
    assert create.status_code == 201, create.text
    aid = create.json()["id"]
    r = admin_client.post(
        f"/v1/admin/accounts/{aid}/test-connection",
        headers={
            "X-CSRF-Token": admin_client.csrf_for(
                f"/v1/admin/accounts/{aid}/test-connection"
            ),
        },
    )
    assert r.status_code == 400, r.text


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError("connection refused"),
        OSError("dns lookup failed"),
        __import__("ssl").SSLError("tls handshake failed"),
        __import__("imaplib").IMAP4.error("login failed"),
        __import__("imapclient").exceptions.LoginError("authentication failed"),
        # oauth2 accounts fail XOAUTH2 at the *token refresh*, before any IMAP
        # traffic. Neither of these is an OSError, so both escaped as a 500
        # until GoogleAuthError joined CONNECT_FAILURE_EXC_TYPES.
        __import__("google.auth.exceptions", fromlist=["x"]).RefreshError(
            "invalid_grant: token revoked"
        ),
        __import__("google.auth.exceptions", fromlist=["x"]).TransportError(
            "could not reach oauth2.googleapis.com"
        ),
    ],
)
def test_test_connection_hard_failure_returns_400_not_500(
    admin_client, monkeypatch, exc
):
    """A genuine connect failure surfaces as a clean 400 on the JSON route,
    mirroring the HTML route — never an opaque 500 (#158)."""
    create = admin_client.post(
        "/v1/admin/accounts",
        json={
            "name": "tc-hard",
            "email_address": "x@y.test",
            "auth_method": "password",
            "imap_host": "imap.example",
            "imap_port": 993,
        },
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/accounts")},
    )
    aid = create.json()["id"]

    def boom(conn, account_id, *, gmail_client_secrets=None):
        raise exc

    monkeypatch.setattr("localmail.serve.admin.accounts_router.svc.probe_connection", boom)
    r = admin_client.post(
        f"/v1/admin/accounts/{aid}/test-connection",
        headers={
            "X-CSRF-Token": admin_client.csrf_for(
                f"/v1/admin/accounts/{aid}/test-connection"
            ),
        },
    )
    assert r.status_code == 400, r.text
    assert str(exc) in r.json()["detail"]


# ---------- unauthenticated ----------

def test_unauthenticated_request_redirects(client_no_auth):
    r = client_no_auth.get("/v1/admin/accounts")
    # require_admin_session raises _AdminRedirect → 303 to /admin/login.
    assert r.status_code in (303, 401, 403)
