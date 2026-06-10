"""RFC 9728 protected-resource discovery helpers for the MCP server."""
import pytest

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


pytest.importorskip("mcp")  # build_protected_resource_routes needs the SDK

from localmail.config import McpConfig  # noqa: E402
from localmail.mcp.discovery import build_protected_resource_routes  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


def test_build_routes_registers_canonical_path():
    routes = build_protected_resource_routes(
        McpConfig(resource_server_url="https://host:8443")
    )
    paths = [r.path for r in routes]
    assert paths == ["/.well-known/oauth-protected-resource/mcp"]


def test_build_routes_serves_expected_document():
    routes = build_protected_resource_routes(
        McpConfig(
            resource_server_url="https://host:8443",
            issuer_url="https://host:8443",
        )
    )
    client = TestClient(Starlette(routes=routes))
    resp = client.get("/.well-known/oauth-protected-resource/mcp")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == "https://host:8443/mcp"
    assert body["authorization_servers"] == ["https://host:8443/"]
    assert body["resource_name"] == "localmail"
    assert body["bearer_methods_supported"] == ["header"]


def test_build_routes_honours_explicit_authorization_servers():
    routes = build_protected_resource_routes(
        McpConfig(
            resource_server_url="https://host:8443",
            authorization_servers=["https://idp.example/"],
        )
    )
    client = TestClient(Starlette(routes=routes))
    body = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert body["authorization_servers"] == ["https://idp.example/"]


def test_build_protected_resource_routes_exported_from_package():
    import localmail.mcp as pkg
    assert hasattr(pkg, "build_protected_resource_routes")
