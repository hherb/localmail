"""HTTP-route tests for the admin OAuth start + callback (Sub-plan 2A)."""

from __future__ import annotations

import keyring
import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.admin.csrf import make_csrf_token
from localmail.api.auth import hash_password
from localmail.config import ServeConfig
from localmail.serve.admin.csrf import csrf_action
from localmail.serve.app import create_app
from tests._fake_google_oauth import FakeFlow


_SIGNING_KEY = "x" * 43
_STATE_KEY = "y" * 43


@pytest.fixture
def serve_cfg() -> ServeConfig:
    return ServeConfig(
        session_signing_key=_SIGNING_KEY,
        state_signing_key=_STATE_KEY,
        oauth_callback_url="https://example.test/admin/oauth/callback",
        cookie_secure=False,  # TestClient uses http://testserver
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
    """TestClient authenticated via the real /admin/login flow.

    Returns a callable ``csrf_for(action)`` attached to the client.
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


@pytest.fixture
def fake_flow(monkeypatch):
    flow = FakeFlow()

    def _capture(*, redirect_uri, client_secrets_file):
        flow.redirect_uri = redirect_uri
        flow.client_secrets_file = client_secrets_file
        return flow

    monkeypatch.setattr(
        'localmail.api.admin.oauth._build_flow',
        _capture,
    )
    return flow


def _create_gmail_account(admin_client) -> str:
    """Create a Gmail OAuth account via the admin API; return id."""
    r = admin_client.post(
        '/v1/admin/accounts',
        json={
            'name': 'gm-http',
            'email_address': 'g@example.test',
            'auth_method': 'oauth2',
            'oauth_provider': 'gmail',
            'imap_host': 'imap.gmail.com',
            'imap_port': 993,
        },
        headers={"X-CSRF-Token": admin_client.csrf_for("/v1/admin/accounts")},
    )
    assert r.status_code == 201, r.text
    return r.json()['id']


def test_oauth_start_returns_consent_url(admin_client, fake_flow):
    aid = _create_gmail_account(admin_client)
    r = admin_client.post(
        f'/v1/admin/accounts/{aid}/oauth/start',
        headers={
            "X-CSRF-Token": admin_client.csrf_for(
                f"/v1/admin/accounts/{aid}/oauth/start"
            ),
        },
    )
    assert r.status_code == 200, r.text
    assert 'accounts.google.com' in r.json()['auth_url']


def test_oauth_callback_round_trip_stores_refresh(admin_client, fake_flow):
    aid = _create_gmail_account(admin_client)
    r1 = admin_client.post(
        f'/v1/admin/accounts/{aid}/oauth/start',
        headers={
            "X-CSRF-Token": admin_client.csrf_for(
                f"/v1/admin/accounts/{aid}/oauth/start"
            ),
        },
    )
    state = r1.json()['auth_url'].split('state=')[1]
    r2 = admin_client.get(
        f'/admin/oauth/callback?state={state}&code=good-code',
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert r2.headers['location'].endswith(
        f'/admin/accounts/{aid}?oauth=success'
    )
    assert keyring.get_password('localmail', 'gm-http:refresh') == 'refresh-xyz'


def test_oauth_callback_failure_redirects_with_failed_flag(
    admin_client, fake_flow
):
    aid = _create_gmail_account(admin_client)
    r1 = admin_client.post(
        f'/v1/admin/accounts/{aid}/oauth/start',
        headers={
            "X-CSRF-Token": admin_client.csrf_for(
                f"/v1/admin/accounts/{aid}/oauth/start"
            ),
        },
    )
    state = r1.json()['auth_url'].split('state=')[1]
    r2 = admin_client.get(
        f'/admin/oauth/callback?state={state}&code=bad-code',
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert 'oauth=failed' in r2.headers['location']
    # Confirm the failed callback did NOT silently store a token.
    assert keyring.get_password('localmail', 'gm-http:refresh') is None
