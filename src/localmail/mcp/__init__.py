"""localmail MCP server (Search Phase 3). Gated by the [mcp] extra."""
from localmail.mcp.discovery import build_protected_resource_routes
from localmail.mcp.server import build_mcp_server


def build_as_provider(pool, *, config, signing_key, consent_path):
    """Construct the OAuth authorization-server provider (behind the [mcp] extra)."""
    from localmail.mcp.oauth.provider import LocalmailASProvider
    return LocalmailASProvider(
        pool, config=config, signing_key=signing_key, consent_path=consent_path
    )


__all__ = ["build_mcp_server", "build_protected_resource_routes", "build_as_provider"]
