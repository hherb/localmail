import pytest

from localmail.config import McpConfig, ServeConfig
from localmail.serve.app import create_app


def _has_route(app, path: str) -> bool:
    return any(getattr(r, "path", None) == path for r in app.router.routes)


def test_consent_route_absent_when_as_disabled(db_dsn, db_conn):
    app = create_app(
        db_dsn=db_dsn, searcher=None, serve_config=ServeConfig(),
        enable_mcp=True, mcp_config=McpConfig(enabled=True),
    )
    assert not _has_route(app, "/oauth/consent")


def test_consent_route_present_when_as_enabled(db_dsn, db_conn):
    app = create_app(
        db_dsn=db_dsn, searcher=None,
        serve_config=ServeConfig(state_signing_key="x" * 32),
        enable_mcp=True,
        mcp_config=McpConfig(enabled=True, authorization_server_enabled=True),
    )
    assert _has_route(app, "/oauth/consent")


def test_as_enabled_without_signing_key_fails_loud(db_dsn, db_conn):
    with pytest.raises(ValueError, match="state_signing_key"):
        create_app(
            db_dsn=db_dsn, searcher=None, serve_config=ServeConfig(),
            enable_mcp=True,
            mcp_config=McpConfig(enabled=True, authorization_server_enabled=True),
        )
