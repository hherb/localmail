# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import time
from urllib.parse import parse_qs, urlparse

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


def _blob(resource=None):
    return encode_consent_state(
        ConsentPayload(
            client_id="cid", redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], state="st", exp=int(time.time()) + 300,
            resource=resource,
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


def test_post_allow_forwards_resource_to_code(consent_client, db_conn):
    r = consent_client.post("/oauth/consent", data={
        "req": _blob(resource="https://h/mcp"), "username": "consent-user",
        "password": "secret-pw", "decision": "allow",
    })
    assert r.status_code == 303
    loc = r.headers["location"]
    code = parse_qs(urlparse(loc).query)["code"][0]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT resource FROM oauth_authorization_codes WHERE code_sha256 = %s",
            (api_auth.hash_token(code),),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "https://h/mcp"


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


def test_unknown_user_still_runs_password_verify(consent_client, monkeypatch):
    import localmail.api.auth as api_auth
    calls = []
    real = api_auth.verify_password

    def spy(pw, h):
        calls.append(h)
        return real(pw, h)

    monkeypatch.setattr(api_auth, "verify_password", spy)
    r = consent_client.post("/oauth/consent", data={
        "req": _blob(), "username": "no-such-user",
        "password": "whatever", "decision": "allow",
    })
    assert r.status_code == 401
    assert calls, "verify_password must run even for an unknown username (timing parity)"
    assert api_auth.DUMMY_PASSWORD_HASH in calls


@pytest.fixture
def proxied_consent_client(db_conn, db_pool):
    """A consent router behind a trusted reverse proxy, with a tight per-IP cap.

    The socket peer is forced to 127.0.0.1 so the resolver sees the loopback
    proxy CIDR as trusted; the originating client then only exists in the
    X-Forwarded-For header, which is the whole point of #220.
    """
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
        auth_config=AuthConfig(
            trusted_proxies=["127.0.0.0/8"],
            login_per_ip_max=2,
            login_per_ip_window_s=60,
            # Keep the other two caps out of the way so only the per-IP one can trip.
            login_per_user_max=100,
            login_global_max=100,
        ),
    )
    app = Starlette(routes=router)
    return TestClient(
        app, follow_redirects=False, client=("127.0.0.1", 50000)
    )


def _bad_login(client, forwarded_for):
    return client.post(
        "/oauth/consent",
        data={
            "req": _blob(), "username": "consent-user",
            "password": "wrong-pw", "decision": "allow",
        },
        headers={"X-Forwarded-For": forwarded_for},
    )


def test_per_ip_cap_buckets_by_the_forwarded_client_not_the_proxy(
    proxied_consent_client,
):
    """#220: the consent login resolved the client IP from the raw socket peer,
    so behind a proxy every user shared one per-IP counter. Three failures from
    three distinct clients tripped a cap of 2 that none of them had reached.
    """
    for i in range(3):
        r = _bad_login(proxied_consent_client, f"203.0.113.{i + 1}")
        assert r.status_code == 401, (
            f"distinct client {i + 1} was throttled by another client's failures "
            f"(got {r.status_code})"
        )


def test_per_ip_cap_still_trips_for_a_repeated_forwarded_client(
    proxied_consent_client,
):
    """The other half: peeling must select a real bucket, not disable the cap."""
    for i in range(2):
        assert _bad_login(proxied_consent_client, "198.51.100.42").status_code == 401, (
            f"failure {i + 1} should be under the cap of 2"
        )
    assert _bad_login(proxied_consent_client, "198.51.100.42").status_code == 429
