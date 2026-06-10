"""localmail MCP server (Search Phase 3). Gated by the [mcp] extra."""
from localmail.mcp.discovery import build_protected_resource_routes
from localmail.mcp.server import build_mcp_server

__all__ = ["build_mcp_server", "build_protected_resource_routes"]
