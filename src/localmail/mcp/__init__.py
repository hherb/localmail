"""localmail MCP server (Search Phase 3). Gated by the [mcp] extra."""
from __future__ import annotations

from typing import TYPE_CHECKING

from localmail.mcp.discovery import build_protected_resource_routes
from localmail.mcp.server import build_mcp_server

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from localmail.config import McpConfig
    from localmail.mcp.oauth.provider import LocalmailASProvider


def build_as_provider(
    pool: ConnectionPool,
    *,
    config: McpConfig,
    signing_key: bytes,
    consent_path: str,
) -> LocalmailASProvider:
    """Construct the OAuth authorization-server provider.

    The `LocalmailASProvider` import is function-local so that taking this
    factory's code path is what pulls in `provider.py` and its `oauth.*`
    submodules — they aren't loaded for a plain opaque-bearer MCP setup. (The
    `[mcp]` extra itself is already required to import this package at all,
    since `server.py` imports the SDK at module top.)
    """
    from localmail.mcp.oauth.provider import LocalmailASProvider
    return LocalmailASProvider(
        pool, config=config, signing_key=signing_key, consent_path=consent_path
    )


__all__ = ["build_mcp_server", "build_protected_resource_routes", "build_as_provider"]
