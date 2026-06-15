import anyio
import pytest
from psycopg_pool import ConnectionPool

pytest.importorskip("mcp")

from mcp.server.auth.provider import AuthorizationParams  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull  # noqa: E402

from localmail.api import auth as api_auth  # noqa: E402
from localmail.config import McpConfig  # noqa: E402
from localmail.mcp.oauth import codes  # noqa: E402
from localmail.mcp.oauth.consent_state import decode_consent_state  # noqa: E402
from localmail.mcp.oauth.provider import LocalmailASProvider  # noqa: E402

SIGNING_KEY = b"provider-test-key"


@pytest.fixture
def db_pool(db_dsn):
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        yield pool
    finally:
        pool.close()


def _provider(pool):
    return LocalmailASProvider(
        pool, config=McpConfig(authorization_server_enabled=True),
        signing_key=SIGNING_KEY, consent_path="/oauth/consent",
    )


def _client(cid="cid", uris=("https://c/cb",)):
    return OAuthClientInformationFull(
        client_id=cid, redirect_uris=list(uris),
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"], token_endpoint_auth_method="none",
    )


def test_register_and_get_client(db_conn, db_pool):
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    got = anyio.run(p.get_client, "cid")
    assert got is not None and got.client_id == "cid"


def test_authorize_returns_consent_redirect_with_signed_blob(db_conn, db_pool):
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    params = AuthorizationParams(
        state="st", scopes=[], code_challenge="chal",
        redirect_uri="https://c/cb", redirect_uri_provided_explicitly=True,
        resource=None,
    )
    url = anyio.run(p.authorize, _client(), params)
    assert url.startswith("/oauth/consent?req=")
    blob = url.split("req=", 1)[1]
    payload = decode_consent_state(blob, key=SIGNING_KEY)
    assert payload.client_id == "cid"
    assert payload.code_challenge == "chal"
    assert payload.redirect_uri == "https://c/cb"


def test_exchange_authorization_code_mints_tokens_and_consumes_code(db_conn, db_pool):
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    with db_pool.connection() as conn:
        uid = api_auth.create_user(conn, "prov-user", "pw")
        raw_code = codes.mint_code(
            conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], ttl_s=60,
        )
        conn.commit()
    loaded = anyio.run(p.load_authorization_code, _client(), raw_code)
    assert loaded is not None and loaded.subject == str(uid)
    token = anyio.run(p.exchange_authorization_code, _client(), loaded)
    assert token.access_token and token.refresh_token
    assert anyio.run(p.load_authorization_code, _client(), raw_code) is None
    at = anyio.run(p.load_access_token, token.access_token)
    assert at is not None and at.subject == str(uid)


def test_exchange_refresh_rotates(db_conn, db_pool):
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    with db_pool.connection() as conn:
        uid = api_auth.create_user(conn, "prov-refresh", "pw")
        raw_code = codes.mint_code(
            conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], ttl_s=60,
        )
        conn.commit()
    loaded = anyio.run(p.load_authorization_code, _client(), raw_code)
    token = anyio.run(p.exchange_authorization_code, _client(), loaded)
    old_refresh = anyio.run(p.load_refresh_token, _client(), token.refresh_token)
    assert old_refresh is not None
    new = anyio.run(p.exchange_refresh_token, _client(), old_refresh, [])
    assert new.refresh_token and new.refresh_token != token.refresh_token
    assert anyio.run(p.load_refresh_token, _client(), token.refresh_token) is None


def test_cross_client_code_rejected(db_conn, db_pool):
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    anyio.run(p.register_client, _client(cid="other", uris=("https://o/cb",)))
    with db_pool.connection() as conn:
        uid = api_auth.create_user(conn, "x-user", "pw")
        raw_code = codes.mint_code(
            conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], ttl_s=60,
        )
        conn.commit()
    # a different client trying to load this code gets None
    assert anyio.run(p.load_authorization_code, _client(cid="other", uris=("https://o/cb",)), raw_code) is None
