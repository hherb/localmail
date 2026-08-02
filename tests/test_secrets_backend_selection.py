# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Choosing between the keyring and the file store.

The keyring stays the default so nothing changes for a deployment that works;
selecting the file backend is what makes a headless host survive a reboot.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from localmail import secrets
from localmail.config import SecretsConfig, load_config


def test_the_default_backend_is_the_keyring(memory_keyring) -> None:
    """An existing install must be untouched by this feature landing."""
    secrets.set_password("acct", "hunter2")
    assert memory_keyring.get_password(secrets.SERVICE, "acct") == "hunter2"


def test_configure_file_routes_writes_to_the_file(tmp_path: Path) -> None:
    secrets.configure(SecretsConfig(backend="file", file_path=tmp_path / "s.json"))
    secrets.set_password("acct", "hunter2")
    assert (tmp_path / "s.json").exists()
    assert secrets.get_password("acct") == "hunter2"


def test_the_file_backend_keeps_the_keyring_untouched(
    tmp_path: Path, memory_keyring
) -> None:
    secrets.configure(SecretsConfig(backend="file", file_path=tmp_path / "s.json"))
    secrets.set_password("acct", "hunter2")
    assert memory_keyring.get_password(secrets.SERVICE, "acct") is None


def test_refresh_tokens_use_the_same_backend(tmp_path: Path) -> None:
    secrets.configure(SecretsConfig(backend="file", file_path=tmp_path / "s.json"))
    secrets.set_refresh_token("acct", "1//0aBc")
    assert secrets.get_refresh_token("acct") == "1//0aBc"
    assert secrets.get_password("acct") is None, (
        "the password and refresh keys must stay distinct in the file store too"
    )


def test_delete_round_trips_through_the_file_backend(tmp_path: Path) -> None:
    secrets.configure(SecretsConfig(backend="file", file_path=tmp_path / "s.json"))
    secrets.set_password("acct", "hunter2")
    secrets.set_refresh_token("acct", "1//0aBc")
    secrets.delete_password("acct")
    assert secrets.get_password("acct") is None
    assert secrets.get_refresh_token("acct") == "1//0aBc"
    secrets.delete_refresh_token("acct")
    assert secrets.get_refresh_token("acct") is None


def test_reset_to_default_restores_the_keyring(
    tmp_path: Path, memory_keyring
) -> None:
    secrets.configure(SecretsConfig(backend="file", file_path=tmp_path / "s.json"))
    secrets.reset_to_default()
    secrets.set_password("acct", "hunter2")
    assert memory_keyring.get_password(secrets.SERVICE, "acct") == "hunter2"


def _write_config(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '[database]\ndsn = "postgresql:///localmail"\n'
        f'[attachments]\nroot = "{tmp_path / "att"}"\n' + extra,
        encoding="utf-8",
    )
    return path


def test_config_defaults_to_the_keyring_backend(tmp_path: Path) -> None:
    cfg = load_config(_write_config(tmp_path, ""))
    assert cfg.secrets.backend == "keyring"


def test_loading_a_config_selects_its_backend(tmp_path: Path) -> None:
    """`load_config` is the only place that knows the resolved config, including
    a `--config PATH` override, so it is where backend selection happens."""
    store = tmp_path / "s.json"
    load_config(
        _write_config(tmp_path, f'[secrets]\nbackend = "file"\nfile_path = "{store}"\n')
    )
    secrets.set_password("acct", "hunter2")
    assert store.exists()


def test_a_default_config_read_cannot_undo_a_named_configs_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The footgun in attaching backend installation to *every* config load.

    Several CLI commands call `load_config()` with no path, so they read the
    default config even under `--config`. Without the pin, that incidental read
    would swap a headless host's `file` backend back to `keyring` mid-command
    and the next secret read would fail with `KeyringLocked` — the exact
    failure this backend exists to remove, reintroduced silently.
    """
    default_cfg = tmp_path / "default" / "config.toml"
    default_cfg.parent.mkdir()
    default_cfg.write_text(
        '[database]\ndsn = "postgresql:///localmail"\n'
        f'[attachments]\nroot = "{tmp_path / "att"}"\n'
        '[secrets]\nbackend = "keyring"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(default_cfg))

    store = tmp_path / "s.json"
    named = _write_config(
        tmp_path, f'[secrets]\nbackend = "file"\nfile_path = "{store}"\n'
    )
    load_config(named)
    assert secrets.active_backend_name() == "file"

    load_config()  # what extract-backfill and friends do
    assert secrets.active_backend_name() == "file"


def test_a_named_config_may_still_replace_another_named_configs_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin protects against an *incidental* default read, not against the
    operator naming a second config on purpose."""
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(tmp_path / "nonexistent.toml"))
    first = _write_config(
        tmp_path, f'[secrets]\nbackend = "file"\nfile_path = "{tmp_path / "s.json"}"\n'
    )
    load_config(first)
    assert secrets.active_backend_name() == "file"

    second = tmp_path / "second.toml"
    second.write_text(
        '[database]\ndsn = "postgresql:///localmail"\n'
        f'[attachments]\nroot = "{tmp_path / "att"}"\n'
        '[secrets]\nbackend = "keyring"\n',
        encoding="utf-8",
    )
    load_config(second)
    assert secrets.active_backend_name() == "keyring"


def test_the_pin_does_not_survive_reset_to_default(tmp_path: Path) -> None:
    """Otherwise the autouse conftest fixture would stop isolating tests after
    the first one that loads a named config."""
    named = _write_config(
        tmp_path, f'[secrets]\nbackend = "file"\nfile_path = "{tmp_path / "s.json"}"\n'
    )
    load_config(named)
    secrets.reset_to_default()
    assert secrets.active_backend_name() == "keyring"


def test_config_expands_a_tilde_in_the_file_path() -> None:
    cfg = SecretsConfig(backend="file", file_path="~/somewhere/secrets.json")
    assert "~" not in str(cfg.file_path)
    assert cfg.file_path.is_absolute()


def test_config_expands_environment_variables_in_the_file_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALMAIL_TEST_SECRET_DIR", "/srv/lm")
    cfg = SecretsConfig(file_path="$LOCALMAIL_TEST_SECRET_DIR/secrets.json")
    assert cfg.file_path == Path("/srv/lm/secrets.json")


def test_an_unknown_backend_is_rejected_at_config_load() -> None:
    """Fail before the process starts, not on the first secret read."""
    with pytest.raises(ValueError):
        SecretsConfig(backend="vault")


def test_add_account_names_the_backend_it_actually_used(
    db_conn, db_dsn: str, tmp_path: Path
) -> None:
    """Reporting "stored ... in keyring" while writing to a file would send an
    operator debugging a headless host to entirely the wrong place."""
    from click.testing import CliRunner

    from localmail.cli import main

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, imap_port, "
            "auth_method) VALUES ('acct', 'a@example.com', 'imap.example.com', "
            "993, 'password')"
        )
    db_conn.commit()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[database]\ndsn = "{db_dsn}"\n\n[attachments]\nroot = "{tmp_path / "att"}"\n'
        f'\n[secrets]\nbackend = "file"\nfile_path = "{tmp_path / "s.json"}"\n',
        encoding="utf-8",
    )
    res = CliRunner().invoke(
        main, ["--config", str(cfg), "add-account", "acct", "--password", "hunter2"]
    )
    assert res.exit_code == 0, res.output
    assert "file" in res.output
    assert "keyring" not in res.output
