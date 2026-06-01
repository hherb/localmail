"""ServeConfig carries the admin signing keys + oauth callback URL."""
from __future__ import annotations

import pytest

from localmail.config import ServeConfig


def test_defaults_are_empty_strings() -> None:
    cfg = ServeConfig()
    assert cfg.session_signing_key == ""
    assert cfg.state_signing_key == ""
    assert cfg.oauth_callback_url == ""


def test_explicit_values_are_kept() -> None:
    cfg = ServeConfig(
        session_signing_key="x" * 43,
        state_signing_key="y" * 43,
        oauth_callback_url="https://localmail.example.com/admin/oauth/callback",
    )
    assert cfg.session_signing_key == "x" * 43
    assert cfg.state_signing_key == "y" * 43
    assert cfg.oauth_callback_url.endswith("/admin/oauth/callback")


@pytest.mark.parametrize("field", ["session_signing_key", "state_signing_key"])
def test_short_keys_rejected(field: str) -> None:
    """Keys shorter than 32 bytes (base64url ~ 43 chars) are footguns."""
    kwargs = {field: "tooshort"}
    with pytest.raises(ValueError):
        ServeConfig(**kwargs)


def test_supervise_daemon_defaults_true() -> None:
    """The serve process supervises `localmail run` by default (2B.4)."""
    assert ServeConfig().supervise_daemon is True


def test_supervise_daemon_can_be_disabled() -> None:
    """Externally-supervised deployments (systemd) set this false."""
    assert ServeConfig(supervise_daemon=False).supervise_daemon is False


def test_runtime_dir_defaults_empty() -> None:
    """Empty runtime_dir means 'resolve from $XDG_RUNTIME_DIR / tmp at run time'."""
    assert ServeConfig().runtime_dir == ""


def test_runtime_dir_is_kept() -> None:
    cfg = ServeConfig(runtime_dir="/run/localmail")
    assert cfg.runtime_dir == "/run/localmail"
