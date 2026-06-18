# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""McpConfig parsing + defaults."""
from localmail.config import Config, McpConfig


def test_mcp_defaults_disabled():
    cfg = McpConfig()
    assert cfg.enabled is False


def test_config_parses_mcp_block():
    cfg = Config.model_validate({
        "database": {"dsn": "postgresql:///x"},
        "mcp": {"enabled": True},
    })
    assert cfg.mcp.enabled is True


def test_config_mcp_defaults_when_absent():
    cfg = Config.model_validate({"database": {"dsn": "postgresql:///x"}})
    assert cfg.mcp.enabled is False


def test_mcp_rejects_malformed_url():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        McpConfig(issuer_url="not-a-url")


def test_mcp_authorization_servers_default_none():
    assert McpConfig().authorization_servers is None


def test_config_parses_mcp_authorization_servers():
    cfg = McpConfig(authorization_servers=["https://idp.example/"])
    assert [str(u) for u in cfg.authorization_servers] == ["https://idp.example/"]


def test_mcp_rejects_malformed_authorization_server_url():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        McpConfig(authorization_servers=["not-a-url"])
