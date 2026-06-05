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
