# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""LocalmailTokenVerifier bridges api_tokens -> MCP AccessToken."""
import asyncio

import pytest
from psycopg_pool import ConnectionPool

pytest.importorskip("mcp")  # the [mcp] extra (mcp SDK) gates this module

from localmail.mcp.auth import (  # noqa: E402
    LocalmailTokenVerifier,
    user_id_from_access_token,
)


# The accepted RFC 8707 audience these tests run the verifier against.
_ACCEPTED = "https://h/mcp"


def _verify(db_dsn, token, *, accepted_resources=(_ACCEPTED,)):
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        verifier = LocalmailTokenVerifier(
            pool, accepted_resources=list(accepted_resources)
        )
        return asyncio.run(verifier.verify_token(token))
    finally:
        pool.close()


@pytest.fixture
def oauth_client(db_conn):
    """The DCR client a bound access token is attributed to."""
    from localmail.mcp.oauth import clients

    clients.register_client(
        db_conn, client_id="cid", client_secret_sha256=None,
        redirect_uris=["https://c/cb"], client_name=None,
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="none", scope=None,
    )
    db_conn.commit()
    return "cid"


def _mint_bound(conn, user_id, resource):
    """An access token as the OAuth AS mints one: bound to ``resource``."""
    from localmail.mcp.oauth import access

    raw = access.mint_access(
        conn, user_id=user_id, client_id="cid", ttl_s=3600, resource=resource
    )
    conn.commit()
    return raw


def test_valid_token_yields_access_token_with_user_id(db_dsn, db_conn, api_user, api_token):
    at = _verify(db_dsn, api_token)
    assert at is not None
    assert user_id_from_access_token(at) == api_user.id


def test_invalid_token_yields_none(db_dsn, db_conn):
    assert _verify(db_dsn, "not-a-real-token") is None


def test_empty_token_yields_none(db_dsn, db_conn):
    assert _verify(db_dsn, "") is None


def test_expired_token_yields_none(db_dsn, db_conn, api_user):
    from localmail.api.auth import issue_token
    raw, _ = issue_token(db_conn, api_user.id, ttl_days=-1)
    db_conn.commit()
    assert _verify(db_dsn, raw) is None


# #401: a token the AS bound to one audience must not pass the opaque-bearer
# verifier just because the AS has since been switched off.
def test_token_bound_to_another_resource_is_refused(db_dsn, db_conn, api_user, oauth_client):
    raw = _mint_bound(db_conn, api_user.id, "https://other/mcp")
    assert _verify(db_dsn, raw) is None


def test_token_bound_to_an_accepted_resource_is_honoured(db_dsn, db_conn, api_user, oauth_client):
    raw = _mint_bound(db_conn, api_user.id, _ACCEPTED)
    at = _verify(db_dsn, raw)
    assert at is not None
    assert user_id_from_access_token(at) == api_user.id


def test_an_unbound_login_token_stays_unrestricted(db_dsn, db_conn, api_user, api_token):
    # Positive control: NULL oauth_resource means unrestricted, so a check
    # that refused every token would fail here rather than pass the two above.
    at = _verify(db_dsn, api_token, accepted_resources=["https://elsewhere/mcp"])
    assert at is not None


def test_accepted_resources_must_be_stated():
    # No default: omitting it would silently skip the audience check (#234).
    with pytest.raises(TypeError):
        LocalmailTokenVerifier(object())  # type: ignore[call-arg, arg-type]


def test_the_opaque_server_checks_the_audience_its_config_derives(
    db_dsn, db_conn, api_user, oauth_client
):
    """End to end through build_mcp_server: AS off, token bound elsewhere."""
    from localmail.config import McpConfig
    from localmail.mcp.server import build_mcp_server

    other = _mint_bound(db_conn, api_user.id, "https://other/mcp")
    ours = _mint_bound(db_conn, api_user.id, _ACCEPTED)
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(
            pool, searcher=None,
            config=McpConfig(enabled=True, resource_server_url="https://h"),
        )
        verifier = server._token_verifier
        assert isinstance(verifier, LocalmailTokenVerifier)
        assert asyncio.run(verifier.verify_token(other)) is None
        assert asyncio.run(verifier.verify_token(ours)) is not None
    finally:
        pool.close()



def _opaque_server(pool, **mcp):
    from localmail.config import McpConfig
    from localmail.mcp.server import build_mcp_server

    return build_mcp_server(
        pool, searcher=None,
        config=McpConfig(enabled=True, resource_server_url="https://h", **mcp),
    )


def test_the_opaque_server_honours_configured_resource_indicators(
    db_dsn, db_conn, api_user, oauth_client
):
    # Configured indicators replace the derived audience, as on the AS path,
    # so a token bound behind a proxy's public URL keeps verifying.
    public = _mint_bound(db_conn, api_user.id, "https://pub/mcp")
    derived = _mint_bound(db_conn, api_user.id, _ACCEPTED)
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        verifier = _opaque_server(
            pool, resource_indicators=["https://pub/mcp"]
        )._token_verifier
        assert asyncio.run(verifier.verify_token(public)) is not None
        assert asyncio.run(verifier.verify_token(derived)) is None
    finally:
        pool.close()


def test_an_all_malformed_resource_indicators_fails_the_opaque_build(db_dsn):
    # A fragment passes pydantic's AnyHttpUrl but canonicalizes to nothing.
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        with pytest.raises(ValueError, match="resource_indicators"):
            _opaque_server(pool, resource_indicators=["https://h/mcp#x"])
    finally:
        pool.close()


def test_an_unbound_oauth_token_stays_unrestricted(
    db_dsn, db_conn, api_user, oauth_client
):
    # NULL oauth_resource is unrestricted for OAuth rows too, not only logins.
    raw = _mint_bound(db_conn, api_user.id, None)
    at = _verify(db_dsn, raw, accepted_resources=["https://elsewhere/mcp"])
    assert at is not None
    assert at.client_id == oauth_client
