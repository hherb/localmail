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
    )
    app = create_app(db_dsn=db_dsn, serve_config=cfg)
    return TestClient(app, follow_redirects=False)


def test_admin_login_csp_allows_scripts_styles_forms(client: TestClient) -> None:
    r = client.get("/admin/login")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "form-action 'self'" in csp


def test_non_admin_route_csp_still_locked_down(client: TestClient) -> None:
    r = client.get("/openapi.json")
    csp = r.headers["content-security-policy"]
    assert "form-action 'none'" in csp
    # script-src must NOT be present (falls back to default-src 'none')
    assert "script-src" not in csp
