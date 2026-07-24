# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""CSP header is relaxed for /admin/* and locked down everywhere else."""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient

from localmail.config import ServeConfig
from localmail.serve.app import create_app


@pytest.fixture
def client(db_dsn):
    cfg = ServeConfig(
        session_signing_key="x" * 43,
        state_signing_key="y" * 43,
        oauth_callback_url="https://example.com/admin/oauth/callback",
        cookie_secure=False,
    )
    app = create_app(db_dsn=db_dsn, serve_config=cfg)
    return TestClient(app, follow_redirects=False)


def test_admin_login_csp_allows_scripts_styles_forms(client: TestClient) -> None:
    r = client.get("/admin/login")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "form-action 'self'" in csp


def test_admin_csp_allows_htmx_xhr_connect_src(client: TestClient) -> None:
    # htmx submits every admin form/button via fetch/XHR, which the browser
    # governs with connect-src. Without an explicit connect-src it falls back
    # to `default-src 'none'` and the browser blocks the request *before it
    # leaves the page* — Save/Store/etc. silently no-op. Regression guard.
    r = client.get("/admin/login")
    csp = r.headers["content-security-policy"]
    assert "connect-src 'self'" in csp


def test_non_admin_route_csp_still_locked_down(client: TestClient) -> None:
    r = client.get("/openapi.json")
    csp = r.headers["content-security-policy"]
    assert "form-action 'none'" in csp
    # script-src must NOT be present (falls back to default-src 'none')
    assert "script-src" not in csp
    # connect-src is admin-only; other paths stay locked to default-src 'none'
    assert "connect-src" not in csp
