# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import pytest
from fastapi.testclient import TestClient

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


def test_consent_csp_allows_self_form_action(db_dsn, db_conn):
    # The consent page POSTs to itself; a real browser blocks the submission
    # unless the CSP permits form-action 'self'. The CSP middleware runs on
    # every response, so even a 400 (missing/invalid req blob) carries the
    # header we assert on.
    app = create_app(
        db_dsn=db_dsn, searcher=None,
        serve_config=ServeConfig(state_signing_key="x" * 32),
        enable_mcp=True,
        mcp_config=McpConfig(enabled=True, authorization_server_enabled=True),
    )
    with TestClient(app) as client:
        resp = client.get("/oauth/consent")
    csp = resp.headers["content-security-policy"]
    assert "form-action 'self'" in csp
    assert "form-action 'none'" not in csp
