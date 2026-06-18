# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Zero-config AS-issuer derivation (Task 12b).

With the OAuth authorization server enabled, the SDK's AS routes are sub-mounted
under /mcp, so the AS-metadata endpoint URLs (derived from AuthSettings.issuer_url)
MUST carry /mcp to resolve. The operator should only set `resource_server_url`;
the issuer is auto-derived from it. These tests prove that the PRM and the AS
metadata both advertise /mcp-carrying endpoints when issuer_url is left default.
"""
import pytest

pytest.importorskip("mcp")

from fastapi.testclient import TestClient

from localmail.config import McpConfig, ServeConfig
from localmail.serve.app import create_app


def _app(db_dsn):
    return create_app(
        db_dsn=db_dsn, searcher=None,
        serve_config=ServeConfig(state_signing_key="x" * 32),
        enable_mcp=True,
        mcp_config=McpConfig(
            enabled=True, authorization_server_enabled=True,
            resource_server_url="http://localhost:9000",  # issuer_url left default
        ),
    )


def test_prm_advertises_mcp_issuer(db_dsn, db_conn):
    with TestClient(_app(db_dsn)) as client:
        prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["authorization_servers"][0].rstrip("/").endswith("/mcp")


def test_as_metadata_endpoints_under_mcp(db_dsn, db_conn):
    with TestClient(_app(db_dsn)) as client:
        meta = client.get("/mcp/.well-known/oauth-authorization-server").json()
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert "/mcp/" in meta[key], (key, meta[key])
