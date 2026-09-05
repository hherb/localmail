# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""build_mcp_server wires five tools onto a FastMCP instance."""
import asyncio

import pytest
from psycopg_pool import ConnectionPool

from localmail.config import McpConfig

pytest.importorskip("mcp")  # the [mcp] extra (mcp SDK) gates this module

from localmail.mcp import build_mcp_server  # noqa: E402


def test_build_registers_expected_tools(db_dsn):
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=None, config=McpConfig(enabled=True))
        names = {t.name for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()
    assert names == {
        "search", "get_message", "get_attachment",
        "list_messages", "list_accounts",
    }


def test_build_disables_dns_rebinding_protection(db_dsn):
    """MCP is served on a network bind, not localhost. The SDK auto-enables a
    localhost-only DNS-rebinding Host allow-list when transport_security is
    unset, which rejects every non-localhost client with 421 Misdirected
    Request. /mcp is bearer-gated and not browser-facing, so it must be turned
    off. Regression for the network-bind 421."""
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=False)
    try:
        server = build_mcp_server(pool, searcher=None, config=McpConfig(enabled=True))
    finally:
        pool.close()
    security = server.settings.transport_security
    assert security is not None
    assert security.enable_dns_rebinding_protection is False


def test_search_tells_the_agent_the_response_names_the_ordering(db_dsn):
    """`sort_applied` must be in the *published* description, not only in
    `tools.py`.

    `mcp/server.py` restates the whole contract for the agent-facing
    schema; `tools.tool_search` is the transport-free wrapper an agent
    never sees. #308 is the precedent — its `sort="rank"` default was fixed
    in `run_search` and had to be fixed here too, because this is the half
    an agent reads. The `sort` description here already tells agents to
    omit `sort` and let the server resolve it, so it must also say where
    the answer comes back.
    """
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=None, config=McpConfig(enabled=True))
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()
    assert "sort_applied" in (tools["search"].description or "")


def test_search_declares_no_sort_default_of_its_own(db_dsn):
    """The MCP tool must not fill in a sort the agent did not ask for.

    An agent pages by calling again with `next_cursor` — the documented
    contract, and the one an unstated default breaks: a "rank" sent on the
    agent's behalf contradicts a date-sorted cursor, which the API answers
    by rejecting the call. Absent, the cursor decides and paging works.
    """
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=None, config=McpConfig(enabled=True))
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()
    sort = (tools["search"].inputSchema or {})["properties"]["sort"]
    assert sort.get("default") is None, (
        f"search states sort={sort.get('default')!r} for the agent: "
        "a cursor's own ordering can never win against it"
    )


def test_search_declares_no_sort_order_default_of_its_own(db_dsn):
    """The MCP tool must not fill in a direction the agent did not ask for.

    server.py restates every parameter for the agent-facing schema, so a
    default written here is sent on the agent's behalf — and a "desc" sent
    that way contradicts every ascending cursor, turning the documented
    paging call into a 400. Fixing run_search alone would not catch this.
    """
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=None, config=McpConfig(enabled=True))
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()
    props = (tools["search"].inputSchema or {})["properties"]
    assert "sort_order" in props, "the search tool does not expose sort_order"
    assert props["sort_order"].get("default") is None, (
        f"search states sort_order={props['sort_order'].get('default')!r} "
        "for the agent: an ascending cursor can never win against it"
    )


def _search_tool_fn(server):
    """The registered `search` tool's underlying function.

    Reached through the tool manager rather than through a transport
    because the hop under test is inside the tool body — the schema
    assertions above see the parameter list and nothing of what the body
    does with it.
    """
    return server._tool_manager.get_tool("search").fn


def test_search_forwards_sort_order_to_the_tool_body(db_dsn, monkeypatch):
    """The `server.py` → `tools.tool_search` hop, which nothing pinned.

    `server.py` restates every parameter for the agent-facing schema and
    then forwards each one by hand, so the schema can declare `sort_order`
    while the body drops it: the agent asks for oldest-first, gets a 200,
    and is handed newest-first. Deleting that one forwarding line left
    every MCP test green.
    """
    import localmail.mcp.server as server_mod

    seen: dict = {}

    def _recording_tool_search(**kwargs):
        seen.update(kwargs)
        return {"results": [], "next_cursor": None}

    monkeypatch.setattr(server_mod.tools, "tool_search", _recording_tool_search)
    monkeypatch.setattr(server_mod, "_current_user_id", lambda: 1)

    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=object(),
                                  config=McpConfig(enabled=True))
        _search_tool_fn(server)(query="invoice", sort="date", sort_order="asc")
    finally:
        pool.close()
    assert seen.get("sort_order") == "asc"
    assert seen.get("sort") == "date"


def test_search_forwards_an_unstated_sort_order_as_none(db_dsn, monkeypatch):
    """Unstated must arrive as None, not as a direction the body invents.

    `run_search` is what resolves an omitted value, and it is also what
    lets a cursor decide instead — so a "desc" manufactured anywhere on
    this path contradicts every ascending cursor and turns the documented
    paging call into a 400.
    """
    import localmail.mcp.server as server_mod

    seen: dict = {}

    def _recording_tool_search(**kwargs):
        seen.update(kwargs)
        return {"results": [], "next_cursor": None}

    monkeypatch.setattr(server_mod.tools, "tool_search", _recording_tool_search)
    monkeypatch.setattr(server_mod, "_current_user_id", lambda: 1)

    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(pool, searcher=object(),
                                  config=McpConfig(enabled=True))
        _search_tool_fn(server)(query="invoice")
    finally:
        pool.close()
    assert "sort_order" in seen
    assert seen["sort_order"] is None
