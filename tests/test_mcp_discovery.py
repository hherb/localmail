"""RFC 9728 protected-resource discovery helpers for the MCP server."""
from pydantic import AnyHttpUrl

from localmail.mcp.discovery import (
    MCP_MOUNT_PATH,
    mcp_resource_url,
    resolve_authorization_servers,
)


def test_mount_path_constant():
    assert MCP_MOUNT_PATH == "/mcp"


def test_mcp_resource_url_appends_mount_path():
    assert mcp_resource_url("https://host:8443") == "https://host:8443/mcp"


def test_mcp_resource_url_is_trailing_slash_safe():
    # pydantic's str(AnyHttpUrl(...)) always yields a trailing slash;
    # the helper must not produce "https://host:8443//mcp".
    assert mcp_resource_url("https://host:8443/") == "https://host:8443/mcp"
    assert mcp_resource_url(str(AnyHttpUrl("https://host:8443"))) == "https://host:8443/mcp"


def test_resolve_authorization_servers_falls_back_to_issuer():
    issuer = AnyHttpUrl("https://host:8443")
    assert resolve_authorization_servers(None, issuer) == [issuer]


def test_resolve_authorization_servers_uses_explicit_list():
    issuer = AnyHttpUrl("https://host:8443")
    configured = [AnyHttpUrl("https://idp.example/")]
    assert resolve_authorization_servers(configured, issuer) == configured


def test_resolve_authorization_servers_empty_list_falls_back():
    # An empty list is meaningless (the field is required to be non-empty);
    # treat it like None so we never emit an empty authorization_servers.
    issuer = AnyHttpUrl("https://host:8443")
    assert resolve_authorization_servers([], issuer) == [issuer]
