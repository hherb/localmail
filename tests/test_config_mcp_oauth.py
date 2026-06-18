# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

from localmail.config import McpConfig


def test_defaults():
    c = McpConfig()
    assert c.authorization_server_enabled is False
    assert c.oauth_access_token_ttl_s == 3600
    assert c.oauth_refresh_token_ttl_s == 2592000
    assert c.oauth_authorization_code_ttl_s == 60
    assert c.oauth_consent_state_ttl_s == 300
    assert c.oauth_registration_window_s == 3600
    assert c.oauth_registration_max == 20
    assert c.oauth_client_unused_retention_s == 86400


def test_override_roundtrip():
    c = McpConfig(authorization_server_enabled=True, oauth_refresh_token_ttl_s=7776000)
    assert c.authorization_server_enabled is True
    assert c.oauth_refresh_token_ttl_s == 7776000
