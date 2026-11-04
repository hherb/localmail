# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""#426 item 4: `filters.attachment_name`, a substring of any file name."""

from __future__ import annotations

import json

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import build_query_string, run_search
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.query import parse_query
from localmail.search.searcher import Searcher


from localmail.api.search_response import search_response

# A real response: the MCP SDK validates tool output against SearchResponse (#358).
_EMPTY_RESPONSE = search_response(
    results=[], next_cursor=None, took_ms=0.0, sort_applied="date",
    rankable=False, rewrite_status="not_requested", rewrite_note=None,
    rewrite_note_code=None)


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def test_the_operator_parses_into_its_filter() -> None:
    parsed = parse_query('attachment_name:"Fee Proposal" invoice')
    assert parsed.filters.attachment_name_substr == "Fee Proposal"
    assert parsed.free_text == "invoice"


def test_the_structured_filter_composes_a_quoted_operator() -> None:
    q = build_query_string(free_text="x", filters={"attachment_name": "a b"})
    assert parse_query(q).filters.attachment_name_substr == "a b"


def test_a_value_that_is_only_quotes_is_refused() -> None:
    with pytest.raises(ValidationFailed, match="attachment_name: value is empty"):
        build_query_string(free_text="", filters={"attachment_name": '""'})


def _seed(conn) -> tuple[int, dict[str, int]]:
    rows = {
        "pdf": [{"filename": "2312 Herb FEE Proposal 230712.pdf", "sha256": "ab" * 32}],
        "other": [{"filename": "photo.jpg", "sha256": "cd" * 32}],
        "none": [],
        # The column has no CHECK: a non-object entry and a non-string name
        # must not raise or match.
        "malformed": ["fee proposal", {"filename": 98765}],
        # `_` and `%` are literal in a file name, not LIKE wildcards.
        "underscore": [{"filename": "fee_proposal.pdf", "sha256": "ef" * 32}],
    }
    ids: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i, (key, atts) in enumerate(rows.items()):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, attachments)"
                " VALUES (%s, %s, %s, 's', 'b', '{}'::jsonb, 'r', 1, %s::jsonb)"
                " RETURNING id",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, json.dumps(atts)),
            )
            ids[key] = cur.fetchone()[0]
    conn.commit()
    return acct, ids


@pytest.fixture
def archive(db_dsn, db_conn):
    acct, ids = _seed(db_conn)
    pool = open_pool(db_dsn)
    try:
        yield Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(),
                       reranker=None, rewriter=None), acct, ids
    finally:
        pool.close()


def _found(archive, value: str, free_text: str = "") -> set[int]:
    searcher, acct, _ = archive
    out = run_search(searcher=searcher, free_text=free_text,
                     filters={"attachment_name": value}, limit=50,
                     allowed_account_ids=[acct], user_id=1)
    return {int(r["message_id"]) for r in out["results"]}


@pytest.mark.parametrize("value", ["fee proposal", "FEE PROPOSAL", "230712.PDF"])
def test_it_matches_any_name_case_insensitively(archive, value) -> None:
    _, _, ids = archive
    assert _found(archive, value) == {ids["pdf"]}


def test_a_non_string_name_is_not_read_as_one(archive) -> None:
    assert _found(archive, "98765") == set()


def test_like_wildcards_are_literal(archive) -> None:
    _, _, ids = archive
    assert _found(archive, "fee_") == {ids["underscore"]}
    assert _found(archive, "%") == set()


def test_it_restricts_a_text_search_too(archive) -> None:
    _, _, ids = archive
    assert _found(archive, "photo", free_text="b") == {ids["other"]}


def test_the_mcp_search_tool_forwards_it(db_dsn, monkeypatch) -> None:
    """Through ``call_tool``, so an unwired parameter cannot pass: the SDK
    drops an argument the tool does not declare (#368)."""
    pytest.importorskip("mcp")
    import asyncio

    from psycopg_pool import ConnectionPool

    import localmail.mcp.server as server_mod
    from localmail.config import McpConfig
    from localmail.mcp import build_mcp_server

    seen: dict = {}

    def _recording_tool_search(**kwargs):
        seen.update(kwargs)
        return _EMPTY_RESPONSE

    monkeypatch.setattr(server_mod.tools, "tool_search", _recording_tool_search)
    monkeypatch.setattr(server_mod, "_current_user_id", lambda: 1)
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=object(),
                                  config=McpConfig(enabled=True))
        asyncio.run(server.call_tool("search", {"attachment_name": "fee"}))
    finally:
        pool.close()
    assert seen["filters"] == {"attachment_name": "fee"}
