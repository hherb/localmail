import anyio
import pytest
from psycopg_pool import ConnectionPool

pytest.importorskip("mcp")

from mcp.server.auth.provider import AuthorizationParams, RefreshToken, TokenError  # noqa: E402
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


def test_exchange_rejects_already_consumed_code(db_conn, db_pool):
    # A code that passed load_authorization_code but was concurrently consumed
    # must not mint a second token set (RFC 6749 single-use).
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
    assert loaded is not None
    # Simulate the racing exchange having already deleted the code.
    with db_pool.connection() as conn:
        assert codes.consume_code(conn, raw_code) is True
        conn.commit()
    with pytest.raises(TokenError) as exc:
        anyio.run(p.exchange_authorization_code, _client(), loaded)
    assert exc.value.error == "invalid_grant"


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


def test_exchange_refresh_rejects_disabled_user_without_500(db_conn, db_pool):
    # A user disabled between load_refresh_token and exchange_refresh_token must
    # fail closed with invalid_grant — never an AssertionError (HTTP 500).
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    with db_pool.connection() as conn:
        uid = api_auth.create_user(conn, "prov-disabled", "pw")
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
    with db_pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE api_users SET disabled_at = now() WHERE id = %s", (uid,)
            )
        conn.commit()
    with pytest.raises(TokenError) as exc:
        anyio.run(p.exchange_refresh_token, _client(), old_refresh, [])
    assert exc.value.error == "invalid_grant"


def test_exchange_refresh_reuse_revokes_family(db_conn, db_pool):
    # Rotate once, then replay the original refresh -> invalid_grant AND the
    # active successor token is dead afterward (RFC 9700 §4.14.2).
    p = _provider(db_pool)
    anyio.run(p.register_client, _client())
    with db_pool.connection() as conn:
        uid = api_auth.create_user(conn, "prov-reuse", "pw")
        raw_code = codes.mint_code(
            conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], ttl_s=60,
        )
        conn.commit()
    loaded = anyio.run(p.load_authorization_code, _client(), raw_code)
    token = anyio.run(p.exchange_authorization_code, _client(), loaded)
    old_refresh = anyio.run(p.load_refresh_token, _client(), token.refresh_token)
    rotated = anyio.run(p.exchange_refresh_token, _client(), old_refresh, [])
    assert anyio.run(p.load_refresh_token, _client(), rotated.refresh_token) is not None
    with pytest.raises(TokenError) as exc:
        anyio.run(p.exchange_refresh_token, _client(), old_refresh, [])
    assert exc.value.error == "invalid_grant"
    assert anyio.run(p.load_refresh_token, _client(), rotated.refresh_token) is None


def _full_flow_tokens(p, db_pool, username):
    """Run code-exchange once; return (access_token, refresh_token, uid)."""
    anyio.run(p.register_client, _client())
    with db_pool.connection() as conn:
        uid = api_auth.create_user(conn, username, "pw")
        raw_code = codes.mint_code(
            conn, client_id="cid", user_id=uid, redirect_uri="https://c/cb",
            redirect_uri_provided_explicitly=True, code_challenge="chal",
            scopes=[], ttl_s=60,
        )
        conn.commit()
    loaded = anyio.run(p.load_authorization_code, _client(), raw_code)
    token = anyio.run(p.exchange_authorization_code, _client(), loaded)
    return token.access_token, token.refresh_token, uid


def test_code_exchange_access_token_is_tagged_with_family(db_conn, db_pool):
    p = _provider(db_pool)
    access_tok, _refresh, _uid = _full_flow_tokens(p, db_pool, "fam-tag-user")
    with db_pool.connection() as conn:
        cur = conn.execute(
            "SELECT oauth_refresh_family_id FROM api_tokens WHERE token_sha256 = %s",
            (api_auth.hash_token(access_tok),),
        )
        row = cur.fetchone()
    assert row is not None and row[0] is not None


def test_refresh_reuse_purges_family_access_tokens(db_conn, db_pool):
    p = _provider(db_pool)
    access_tok, refresh_tok, _uid = _full_flow_tokens(p, db_pool, "reuse-user")
    # access token works before reuse
    assert anyio.run(p.load_access_token, access_tok) is not None
    # rotate once (consumes refresh_tok)
    rt = anyio.run(p.load_refresh_token, _client(), refresh_tok)
    anyio.run(p.exchange_refresh_token, _client(), rt, [])
    # replay the now-consumed original refresh token -> reuse -> TokenError
    with pytest.raises(TokenError):
        rt2 = RefreshToken(
            token=refresh_tok, client_id="cid", scopes=[], expires_at=None
        )
        anyio.run(p.exchange_refresh_token, _client(), rt2, [])
    # the access token minted in that family is gone
    assert anyio.run(p.load_access_token, access_tok) is None


def test_reuse_purge_spares_login_token_of_same_user(db_conn, db_pool):
    p = _provider(db_pool)
    access_tok, refresh_tok, uid = _full_flow_tokens(p, db_pool, "spare-user")
    with db_pool.connection() as conn:
        login_tok, _exp = api_auth.issue_token(conn, uid)  # NULL family
        conn.commit()
    rt = anyio.run(p.load_refresh_token, _client(), refresh_tok)
    anyio.run(p.exchange_refresh_token, _client(), rt, [])
    with pytest.raises(TokenError):
        rt2 = RefreshToken(
            token=refresh_tok, client_id="cid", scopes=[], expires_at=None
        )
        anyio.run(p.exchange_refresh_token, _client(), rt2, [])
    # OAuth access token purged, but the user's login token (NULL family) survives
    assert anyio.run(p.load_access_token, access_tok) is None
    with db_pool.connection() as conn:
        assert api_auth.verify_token(conn, login_tok) is not None


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
