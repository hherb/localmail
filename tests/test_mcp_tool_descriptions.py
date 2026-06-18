# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The agent-facing MCP tool + parameter descriptions are the contract an MCP
client (e.g. Claude) reads to decide *which* tool to call and *how* to fill its
arguments. These tests pin that every tool and every parameter carries
substantive guidance — not just that the five tools exist (that's
`test_mcp_server_build.py`).
"""
import asyncio

import pytest
from psycopg_pool import ConnectionPool

from localmail.config import McpConfig

pytest.importorskip("mcp")  # the [mcp] extra (mcp SDK) gates this module

from localmail.mcp import build_mcp_server  # noqa: E402


@pytest.fixture
def tools_by_name(db_dsn):
    pool = ConnectionPool(db_dsn, min_size=1, max_size=2, open=True)
    try:
        server = build_mcp_server(
            pool, searcher=None, config=McpConfig(enabled=True))
        yield {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        pool.close()


def _params(tool) -> dict:
    return (tool.inputSchema or {}).get("properties", {})


def test_every_parameter_is_documented(tools_by_name):
    """No tool exposes an undocumented parameter to an agent."""
    missing = [
        f"{name}.{pname}"
        for name, tool in tools_by_name.items()
        for pname, pschema in _params(tool).items()
        if not (pschema.get("description") or "").strip()
    ]
    assert not missing, f"undocumented MCP tool parameters: {missing}"


def test_every_tool_states_acl_scoping(tools_by_name):
    """Each tool tells the agent results are limited to granted accounts."""
    for name, tool in tools_by_name.items():
        desc = (tool.description or "").lower()
        assert any(term in desc for term in ("acl", "granted", "allowed")), (
            f"{name} description omits ACL scoping: {tool.description!r}")


def test_descriptions_carry_when_to_use_guidance(tools_by_name):
    """Spot-check the load-bearing behavioural/security invariants agents need."""
    search = (tools_by_name["search"].description or "").lower()
    assert "cursor" in search, "search must document cursor paging"

    get_attachment = (tools_by_name["get_attachment"].description or "").lower()
    assert "never" in get_attachment and "bytes" in get_attachment, (
        "get_attachment must state it never returns raw bytes")

    list_messages = (tools_by_name["list_messages"].description or "").lower()
    assert "browse" in list_messages, (
        "list_messages must describe itself as a browse (no-query) path")


def test_search_filter_params_are_each_documented(tools_by_name):
    """The many search filters each carry their own description (not one blob)."""
    params = _params(tools_by_name["search"])
    for pname in (
        "query", "sort", "limit", "cursor", "account_ids", "folder_ids",
        "date_from", "date_to", "from_addr", "to", "subject",
        "has_attachment", "lang",
    ):
        assert (params[pname].get("description") or "").strip(), (
            f"search.{pname} is undocumented")


def test_search_tool_exposes_documented_smart_param(tools_by_name):
    params = _params(tools_by_name["search"])
    assert "smart" in params, "search tool must expose a smart param"
    assert (params["smart"].get("description") or "").strip(), \
        "search.smart must be documented"
