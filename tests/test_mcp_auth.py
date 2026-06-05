"""LocalmailTokenVerifier bridges api_tokens -> MCP AccessToken."""
import asyncio

from psycopg_pool import ConnectionPool

from localmail.mcp.auth import LocalmailTokenVerifier, user_id_from_access_token


def _verify(db_dsn, token):
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        return asyncio.run(LocalmailTokenVerifier(pool).verify_token(token))
    finally:
        pool.close()


def test_valid_token_yields_access_token_with_user_id(db_dsn, db_conn, api_user, api_token):
    at = _verify(db_dsn, api_token)
    assert at is not None
    assert user_id_from_access_token(at) == api_user.id


def test_invalid_token_yields_none(db_dsn, db_conn):
    assert _verify(db_dsn, "not-a-real-token") is None


def test_empty_token_yields_none(db_dsn, db_conn):
    assert _verify(db_dsn, "") is None
