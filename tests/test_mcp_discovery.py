"""RFC 9728 protected-resource discovery helpers for the MCP server."""
import pytest

# Importing `localmail.mcp.discovery` executes `localmail/mcp/__init__.py`, which
# eagerly imports the SDK-bound `server` module — so the whole file (even the
# pure-helper tests) needs the [mcp] extra. Gate at the top, like the sibling
# test_mcp_* files, so the bare suite skips cleanly instead of erroring on
# collection.
pytest.importorskip("mcp")

from pydantic import AnyHttpUrl  # noqa: E402

from localmail.mcp.discovery import (  # noqa: E402
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


def test_challenge_resource_metadata_matches_canonical_url():
    from unittest.mock import MagicMock

    from localmail.mcp import build_mcp_server

    server = build_mcp_server(
        MagicMock(),  # pool is only used by the token verifier, never reached on a 401
        searcher=None,
        config=McpConfig(enabled=True, resource_server_url="https://host:8443"),
    )
    client = TestClient(server.streamable_http_app())
    resp = client.post(
        "/",
        json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 401
    challenge = resp.headers["www-authenticate"]
    assert (
        'resource_metadata="https://host:8443/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )


_PRM_PATH = "/.well-known/oauth-protected-resource/mcp"


def _has_prm_route(app) -> bool:
    return any(getattr(r, "path", None) == _PRM_PATH for r in app.routes)


def test_prm_route_present_when_mcp_enabled(db_dsn):
    from localmail.serve.app import create_app

    app = create_app(
        db_dsn=db_dsn, enable_mcp=True, mcp_config=McpConfig(enabled=True)
    )
    try:
        assert _has_prm_route(app)
    finally:
        app.state.pool.close()


def test_prm_route_absent_by_default(db_dsn):
    from localmail.serve.app import create_app

    app = create_app(db_dsn=db_dsn)
    try:
        assert not _has_prm_route(app)
    finally:
        app.state.pool.close()


def test_prm_route_served_publicly_through_full_app(db_dsn):
    # End-to-end: GET the canonical PRM path through the real create_app stack
    # (middleware included). It must be reachable without auth and return the
    # metadata document — a route merely being present in app.routes wouldn't
    # catch a middleware that shadows or rejects it.
    from localmail.serve.app import create_app

    app = create_app(
        db_dsn=db_dsn,
        enable_mcp=True,
        mcp_config=McpConfig(
            enabled=True, resource_server_url="https://host:8443"
        ),
    )
    try:
        resp = TestClient(app).get(_PRM_PATH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["resource"] == "https://host:8443/mcp"
        assert body["resource_name"] == "localmail"
    finally:
        app.state.pool.close()
