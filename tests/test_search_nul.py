# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A NUL byte in a search query or text filter is refused, never a 500 (#377).

Postgres ``TEXT`` cannot hold U+0000, so a NUL that reaches a query parameter
makes psycopg raise ``DataError`` — not an ``APIError``, so ``serve.app``
answered ``/v1/search`` with an unhandled 500, and the MCP tool raised an
exception no ``ToolError`` mapping covers. JSON can carry ``\\u0000`` and
pydantic's ``str`` accepts it, so this was reachable from the wire.

Refused rather than stripped: a stripped query is a different query, answered
200 as if it were the one asked — the silence #364 ("honoured or refused,
never dropped") exists to end. The rule lives in ``parse_query``, which every
search path runs before any IO, so the HTTP gate, the MCP tool and a library
caller of ``Searcher.search`` all refuse it the same way.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from localmail.api.errors import ValidationFailed
from localmail.api.search import run_search
from localmail.search.query import QueryParseError, parse_query
from localmail.serve.app import create_app

NUL = "\x00"

#: (query string, the field the refusal must name). Every operator that
#: reaches SQL as text is named after itself; anything else is the query's.
PARSER_CASES = [
    (f"inv{NUL}oice", "query"),
    (f"invoice from:a{NUL}b", "from"),
    (f'from:"a{NUL}b" invoice', "from"),
    (f"to:a{NUL}b", "to"),
    (f"subject:a{NUL}b", "subject"),
    (f"lang:e{NUL}n", "lang"),
    (f"label:a{NUL}b", "label"),
    (f"account:a{NUL}b", "account"),
    (f"folder:a{NUL}b", "folder"),
    # Not an operator, so the whole token is free text.
    (f"fr{NUL}om:alice", "query"),
    # Refused before its value is read, so not as the free text an
    # unparseable id would otherwise fall back to.
    (f"account_id:1{NUL}", "account_id"),
    (NUL, "query"),
]


@pytest.mark.parametrize(("query", "field"), PARSER_CASES)
def test_the_parser_refuses_a_nul_naming_its_field(query: str, field: str) -> None:
    with pytest.raises(QueryParseError) as excinfo:
        parse_query(query)
    assert str(excinfo.value).startswith(f"{field}: ")
    assert "NUL" in str(excinfo.value)


def test_a_query_without_a_nul_still_parses() -> None:
    """The positive control: a rule refusing everything passes every case
    above."""
    parsed = parse_query('invoice from:"a b" lang:en')
    assert parsed.free_text == "invoice"
    assert parsed.filters.from_substr == "a b"


def _searcher() -> MagicMock:
    """A searcher that fails the test if the refusal comes too late."""
    s = MagicMock()
    s.search.side_effect = AssertionError("retrieval must not start")
    return s


#: (free_text, filters, field) — the five wire inputs #377 names.
WIRE_CASES = [
    (f"inv{NUL}oice", {}, "query"),
    ("", {"from": f"a{NUL}b"}, "from"),
    ("x", {"to": f"a{NUL}b"}, "to"),
    ("x", {"subject": f"a{NUL}b"}, "subject"),
    ("x", {"lang": f"e{NUL}n"}, "lang"),
]


@pytest.mark.parametrize(("free_text", "filters", "field"), WIRE_CASES)
@pytest.mark.parametrize("acl", [[], [1]], ids=["empty-acl", "granted"])
def test_run_search_refuses_a_nul_before_any_work(
    free_text: str, filters: dict, field: str, acl: list[int],
) -> None:
    """Including from a caller granted nothing: the empty-ACL branch answers
    with an empty page, which would report the request as completed."""
    with pytest.raises(ValidationFailed) as excinfo:
        run_search(searcher=_searcher(), free_text=free_text, filters=filters,
                   limit=10, allowed_account_ids=acl, user_id=1)
    assert str(excinfo.value).startswith(f"{field}: ")


@pytest.mark.parametrize(("free_text", "filters", "field"), WIRE_CASES)
def test_the_http_route_answers_a_nul_with_a_400(
    db_dsn: str, api_token: str, free_text: str, filters: dict, field: str,
) -> None:
    app = create_app(db_dsn=db_dsn, searcher=_searcher())
    with TestClient(app) as c:
        r = c.post("/v1/search",
                   json={"query": free_text, "filters": filters, "limit": 5},
                   headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "/problems/validation-failed"
    assert body["detail"].startswith(f"{field}: ")


#: The same five, spelled as MCP ``search`` arguments.
MCP_CASES = [
    ({"query": f"inv{NUL}oice"}, "query"),
    ({"from_addr": f"a{NUL}b"}, "from"),
    ({"to": f"a{NUL}b"}, "to"),
    ({"subject": f"a{NUL}b"}, "subject"),
    ({"lang": f"e{NUL}n"}, "lang"),
]


@pytest.mark.parametrize(("arguments", "field"), MCP_CASES)
def test_the_mcp_tool_answers_a_nul_with_a_tool_error(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch, arguments: dict, field: str,
) -> None:
    """Through ``call_tool`` and the real ``tool_search``, so the mapping that
    is exercised is the one an agent meets."""
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError
    from psycopg_pool import ConnectionPool

    import localmail.mcp.server as server_mod
    from localmail.config import McpConfig
    from localmail.mcp import build_mcp_server

    monkeypatch.setattr(server_mod, "_current_user_id", lambda: 1)
    monkeypatch.setattr(server_mod, "allowed_account_ids", lambda conn, uid: [])
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=_searcher(),
                                  config=McpConfig(enabled=True))
        with pytest.raises(ToolError) as excinfo:
            asyncio.run(server.call_tool("search", arguments))
    finally:
        pool.close()
    assert f"{field}: " in str(excinfo.value)
