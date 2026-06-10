"""RFC 9728 protected-resource discovery for the MCP server.

Pure helpers (no IO) that derive the resource URL and authorization-server list,
plus one thin wrapper over the SDK's `create_protected_resource_routes`. The SDK
import is function-level so this module's top level stays import-safe; the
package still gates the SDK behind the `mcp` extra via `localmail.mcp` (see
`__init__`).

localmail is **not** an OAuth authorization server: tokens are opaque bearers
obtained out-of-band via `/v1/auth/login`. This surface only lets a spec-strict
client *discover* that `/mcp` is a protected resource (the WWW-Authenticate
challenge + the metadata document), nothing more.
"""
from __future__ import annotations

from pydantic import AnyHttpUrl

from localmail.config import McpConfig

# The path FastMCP is mounted at inside `localmail serve` (app.mount("/mcp", …)).
# Kept here so the RFC 9728 resource URL and the SDK-derived metadata route path
# stay consistent.
MCP_MOUNT_PATH = "/mcp"

# RFC 9728 `resource_name` advertised in the metadata document.
RESOURCE_NAME = "localmail"


def mcp_resource_url(base_url: str) -> str:
    """The RFC 9728 resource identifier: the public origin + the MCP mount path.

    `base_url` is `McpConfig.resource_server_url` stringified — pydantic's
    `AnyHttpUrl` always renders a trailing slash, so strip it before appending
    to avoid a doubled separator.
    """
    return base_url.rstrip("/") + MCP_MOUNT_PATH


def resolve_authorization_servers(
    configured: list[AnyHttpUrl] | None, issuer_url: AnyHttpUrl
) -> list[AnyHttpUrl]:
    """The metadata's required `authorization_servers` list.

    Falls back to `[issuer_url]` when the operator configured nothing (None or
    an empty list); an explicit non-empty list wins.
    """
    return configured if configured else [issuer_url]
