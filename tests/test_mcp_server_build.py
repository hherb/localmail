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
