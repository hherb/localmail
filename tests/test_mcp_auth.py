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


def test_expired_token_yields_none(db_dsn, db_conn, api_user):
    from localmail.api.auth import issue_token
    raw, _ = issue_token(db_conn, api_user.id, ttl_days=-1)
    db_conn.commit()
    assert _verify(db_dsn, raw) is None
