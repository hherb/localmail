# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Text Postgres cannot take is refused in a search query, never a 500 (#377).

Two shapes. Postgres ``TEXT`` cannot hold U+0000, so a NUL reaching a query
parameter makes psycopg raise ``DataError``; and a lone surrogate (JSON
``"\\ud800"`` decodes to one, and pydantic's ``str`` accepts it) cannot be
encoded as UTF-8, so psycopg raises ``UnicodeEncodeError``. Neither is an
``APIError``, so ``serve.app`` answered ``/v1/search`` with an unhandled 500,
and the MCP tool surfaced FastMCP's generic ``ToolError`` carrying psycopg's
raw text.

Refused rather than stripped: a stripped query is a different query, answered
200 as if it were the one asked — the silence #364 ("honoured or refused,
never dropped") exists to end. The rule lives in ``parse_query``, which every
search path runs before any IO, so the HTTP gate, the MCP tool and a library
caller of ``Searcher.search`` all refuse it the same way.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from localmail.api.errors import ValidationFailed
from localmail.api.search import run_search
from localmail.config import SearchConfig
from localmail.search.query import QueryParseError, parse_query
from localmail.search.searcher import Searcher
from localmail.serve.app import create_app

NUL = "\x00"
SURROGATE = "\ud800"

#: (bad character, the phrase its refusal must carry). The phrase is what
#: tells this refusal from another one sharing the ``field: `` prefix, such
#: as ``has: expected …`` or ``after: expected YYYY-MM-DD``.
BAD = [
    pytest.param(NUL, "NUL character (U+0000)", id="nul"),
    pytest.param(SURROGATE, "lone surrogate (U+D800)", id="surrogate"),
]

#: (query template, the field the refusal must name). ``{c}`` is the bad
#: character. Every operator is named after itself; anything else is the
#: query's.
PARSER_CASES = [
    ("inv{c}oice", "query"),
    ('"inv{c}oice report"', "query"),
    ("invoice from:a{c}b", "from"),
    ('from:"a{c}b" invoice', "from"),
    ("FROM:a{c}b", "from"),
    ("to:a{c}b", "to"),
    ("subject:a{c}b", "subject"),
    ("lang:e{c}n", "lang"),
    ("label:a{c}b", "label"),
    ("account:a{c}b", "account"),
    ("folder:a{c}b", "folder"),
    # These four have refusals of their own that share the prefix, so the
    # phrase check is what proves this rule ran first.
    ("has:att{c}", "has"),
    ("after:2020-01-0{c}", "after"),
    ("before:2020-01-0{c}", "before"),
    ("folder_id:1{c}", "folder_id"),
    # Not an operator, so the whole token is free text.
    ("fr{c}om:alice", "query"),
    # Refused before its value is read, so not as the free text an
    # unparseable id would otherwise fall back to.
    ("account_id:1{c}", "account_id"),
    ("{c}", "query"),
]


@pytest.mark.parametrize(("bad", "phrase"), BAD)
@pytest.mark.parametrize(("template", "field"), PARSER_CASES)
def test_the_parser_refuses_naming_its_field(
    template: str, field: str, bad: str, phrase: str,
) -> None:
    with pytest.raises(QueryParseError) as excinfo:
        parse_query(template.format(c=bad))
    assert str(excinfo.value).startswith(f"{field}: contains a {phrase}")


def test_a_query_without_a_bad_character_still_parses() -> None:
    """The positive control: a rule refusing everything passes every case
    above. Non-ASCII text that IS encodable must still parse."""
    parsed = parse_query('invoice from:"a b" lang:en Grüße 😀')
    assert parsed.free_text == "invoice Grüße 😀"
    assert parsed.filters.from_substr == "a b"


def test_a_library_caller_of_searcher_gets_a_named_error_before_any_io() -> None:
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("no connection may be opened")
    embeddings = MagicMock()
    embeddings.embed_query.side_effect = AssertionError("nothing may be embedded")
    s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=embeddings,
                 reranker=None, rewriter=None)
    with pytest.raises(QueryParseError, match=r"^query: contains a NUL"):
        s.search(f"inv{NUL}oice", allowed_account_ids=None)
    pool.connection.assert_not_called()


def _searcher() -> MagicMock:
    """A searcher that fails the test if the refusal comes too late."""
    s = MagicMock()
    s.search.side_effect = AssertionError("retrieval must not start")
    return s


#: (free_text template, filters template, field) — the five wire inputs #377
#: names, plus a value that is nothing but the bad character.
WIRE_CASES = [
    ("inv{c}oice", {}, "query"),
    ("", {"from": "a{c}b"}, "from"),
    ("", {"from": "{c}"}, "from"),
    ("x", {"to": "a{c}b"}, "to"),
    ("x", {"subject": "a{c}b"}, "subject"),
    ("x", {"lang": "e{c}n"}, "lang"),
]


def _fill(free_text: str, filters: dict, bad: str) -> tuple[str, dict]:
    return (free_text.format(c=bad),
            {k: v.format(c=bad) for k, v in filters.items()})


@pytest.mark.parametrize(("bad", "phrase"), BAD)
@pytest.mark.parametrize(("free_text", "filters", "field"), WIRE_CASES)
@pytest.mark.parametrize("acl", [[], [1]], ids=["empty-acl", "granted"])
def test_run_search_refuses_before_any_work(
    free_text: str, filters: dict, field: str, acl: list[int],
    bad: str, phrase: str,
) -> None:
    """Including from a caller granted nothing: the empty-ACL branch answers
    with an empty page, which would report the request as completed."""
    free_text, filters = _fill(free_text, filters, bad)
    with pytest.raises(ValidationFailed) as excinfo:
        run_search(searcher=_searcher(), free_text=free_text, filters=filters,
                   limit=10, allowed_account_ids=acl, user_id=1)
    assert str(excinfo.value).startswith(f"{field}: contains a {phrase}")


@pytest.mark.parametrize(("bad", "phrase"), BAD)
@pytest.mark.parametrize(("free_text", "filters", "field"), WIRE_CASES)
def test_the_http_route_answers_with_a_400(
    db_dsn: str, api_token: str, free_text: str, filters: dict, field: str,
    bad: str, phrase: str,
) -> None:
    free_text, filters = _fill(free_text, filters, bad)
    app = create_app(db_dsn=db_dsn, searcher=_searcher())
    # Encoded by hand: ``json.dumps`` escapes a lone surrogate as ``\ud800``,
    # which is how a real client sends one, where a UTF-8 encoder would
    # refuse it before the request left the test.
    body = json.dumps({"query": free_text, "filters": filters, "limit": 5})
    with TestClient(app) as c:
        r = c.post("/v1/search", content=body.encode("ascii"),
                   headers={"Authorization": f"Bearer {api_token}",
                            "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")
    problem = r.json()
    assert problem["type"] == "/problems/validation-failed"
    assert problem["detail"].startswith(f"{field}: contains a {phrase}")


#: The wire cases, spelled as MCP ``search`` arguments.
MCP_CASES = [
    ({"query": "inv{c}oice"}, "query"),
    ({"from_addr": "a{c}b"}, "from"),
    ({"to": "a{c}b"}, "to"),
    ({"subject": "a{c}b"}, "subject"),
    ({"lang": "e{c}n"}, "lang"),
]


@pytest.mark.parametrize(("bad", "phrase"), BAD)
@pytest.mark.parametrize(("arguments", "field"), MCP_CASES)
def test_the_mcp_tool_answers_with_a_tool_error(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch, arguments: dict, field: str,
    bad: str, phrase: str,
) -> None:
    """Through ``call_tool`` and the real ``tool_search``, so the mapping that
    is exercised is the one an agent meets."""
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError
    from psycopg_pool import ConnectionPool

    import localmail.mcp.server as server_mod
    from localmail.config import McpConfig
    from localmail.mcp import build_mcp_server

    arguments = {k: v.format(c=bad) for k, v in arguments.items()}
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
    # FastMCP prefixes "Error executing tool search: ", so anchor on the
    # separator ahead of the field rather than on the start of the string.
    assert f": {field}: contains a {phrase}" in str(excinfo.value)
