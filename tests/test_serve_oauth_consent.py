import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from psycopg_pool import ConnectionPool

pytest.importorskip("mcp")

from localmail.api import auth as api_auth  # noqa: E402
from localmail.config import AuthConfig, McpConfig  # noqa: E402
from localmail.mcp.oauth import clients  # noqa: E402
from localmail.mcp.oauth.consent_state import ConsentPayload, encode_consent_state  # noqa: E402

KEY = b"consent-router-key"


@pytest.fixture
def db_pool(db_dsn):
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def consent_client(db_conn, db_pool):
    from localmail.serve.oauth.consent_router import build_consent_router

    clients.register_client(
        db_conn, client_id="cid", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name="C",
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    api_auth.create_user(db_conn, "consent-user", "secret-pw")
    api_auth.reset_login_rate_limiter(db_conn)
    db_conn.commit()
    router = build_consent_router(
        pool=db_pool, signing_key=KEY,
        mcp_config=McpConfig(authorization_server_enabled=True),
        auth_config=AuthConfig(),
    )
    app = Starlette(routes=router)
    return TestClient(app, follow_redirects=False)


def _blob():
    return encode_consent_state(
        ConsentPayload(
            client_id="cid", redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], state="st", exp=int(time.time()) + 300,
        ),
        key=KEY,
    )


def test_get_renders_form(consent_client):
    r = consent_client.get("/oauth/consent", params={"req": _blob()})
    assert r.status_code == 200
    assert "password" in r.text.lower()


def test_post_allow_with_valid_credentials_redirects_with_code(consent_client):
    r = consent_client.post("/oauth/consent", data={
        "req": _blob(), "username": "consent-user",
        "password": "secret-pw", "decision": "allow",
    })
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://c/cb?")
    assert "code=" in loc and "state=st" in loc


def test_post_deny_redirects_with_error(consent_client):
    r = consent_client.post("/oauth/consent", data={"req": _blob(), "decision": "deny"})
    assert r.status_code == 303
    assert "error=access_denied" in r.headers["location"]
    assert "state=st" in r.headers["location"]


def test_post_allow_bad_password_rerenders_with_error(consent_client):
    r = consent_client.post("/oauth/consent", data={
        "req": _blob(), "username": "consent-user",
        "password": "wrong", "decision": "allow",
    })
    assert r.status_code == 401
    assert "incorrect" in r.text.lower() or "invalid" in r.text.lower()


def test_post_tampered_blob_rejected(consent_client):
    r = consent_client.post("/oauth/consent", data={
        "req": "tampered.blob", "username": "consent-user",
        "password": "secret-pw", "decision": "allow",
    })
    assert r.status_code == 400
