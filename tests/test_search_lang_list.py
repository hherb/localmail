# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`lang` takes a list on the wire, HTTP and MCP alike (#370).

`api.search._filter_tokens` always unioned a list of languages, but both
transports declared `str | None`, so the list form was a schema error and
the union reachable only from library callers. These pins drive the real
transports; the token rules themselves are pinned in
`test_api_search_lang_dates.py`.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from localmail.config import McpConfig
from localmail.mcp.server import build_mcp_server
from localmail.serve.app import create_app
from tests.test_serve_search_route import (
    _fake_searcher_returning_one_hit,
    _seed_acct_and_grant,
)


def _post(db_dsn, api_token, filters):
    searcher = _fake_searcher_returning_one_hit()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=searcher))
    r = c.post(
        "/v1/search",
        json={"query": "x", "filters": filters, "limit": 5},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    return r, searcher


def test_a_lang_list_reaches_the_searcher_as_one_token_per_language(
    db_dsn, api_token, db_conn, api_user,
):
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token, {"lang": ["en", "de"]})
    assert r.status_code == 200, r.text
    query = searcher.search.call_args.args[0]
    assert "lang:en" in query.split()
    assert "lang:de" in query.split()


def test_a_single_lang_string_still_works(db_dsn, api_token, db_conn, api_user):
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token, {"lang": "en"})
    assert r.status_code == 200, r.text
    assert "lang:en" in searcher.search.call_args.args[0].split()


def test_a_list_member_that_would_retokenize_is_a_400(
    db_dsn, api_token, db_conn, api_user,
):
    """A bad member is refused by the same rule as a bad single string,
    not dropped while its siblings are honoured."""
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token, {"lang": ["en", "e n"]})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    assert "lang: expected a language code" in r.json()["detail"]
    searcher.search.assert_not_called()


def test_a_non_string_list_member_is_a_400(db_dsn, api_token, db_conn, api_user):
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token, {"lang": ["en", 3]})
    assert r.status_code == 400
    assert "filters.lang" in r.json()["detail"]
    searcher.search.assert_not_called()


def _mcp_server(db_dsn):
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    return pool, build_mcp_server(pool, searcher=object(),
                                  config=McpConfig(enabled=True))


def test_the_mcp_search_tool_forwards_a_lang_list(db_dsn, monkeypatch):
    """Through ``call_tool``: the SDK's argument model is what would refuse
    a list against a ``str`` annotation."""
    import localmail.mcp.server as server_mod

    seen: dict = {}

    def _recording_tool_search(**kwargs):
        seen.update(kwargs)
        return {"results": [], "next_cursor": None}

    monkeypatch.setattr(server_mod.tools, "tool_search", _recording_tool_search)
    monkeypatch.setattr(server_mod, "_current_user_id", lambda: 1)

    pool, server = _mcp_server(db_dsn)
    try:
        asyncio.run(server.call_tool("search", {"lang": ["en", "de"]}))
    finally:
        pool.close()
    assert seen["filters"] == {"lang": ["en", "de"]}


def test_the_published_mcp_schema_admits_a_lang_list(db_dsn):
    pool, server = _mcp_server(db_dsn)
    try:
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()
    lang = (tools["search"].inputSchema or {})["properties"]["lang"]
    assert {"type": "array", "items": {"type": "string"}} in lang["anyOf"]
