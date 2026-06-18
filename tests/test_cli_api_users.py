# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""CLI tests for api-user management.

We bypass the TOML config path by setting LOCALMAIL_DSN_OVERRIDE; the
add/remove/list commands consult this env var first via a helper added
to cli.py.
"""
import os
from pathlib import Path

import psycopg
from click.testing import CliRunner

from localmail.cli import main


def _make_min_config(tmp_path: Path, dsn: str) -> Path:
    """Minimal TOML config for the CLI's --config arg."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[database]\ndsn = "{dsn}"\n\n[attachments]\nroot = "{tmp_path / "att"}"\n'
    )
    return cfg


def test_add_then_list_then_remove(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()

    r = runner.invoke(
        main,
        ["--config", str(cfg), "add-api-user", "alice", "--password", "hunter2"],
        env=env,
    )
    assert r.exit_code == 0, r.output

    r = runner.invoke(main, ["--config", str(cfg), "list-api-users"], env=env)
    assert r.exit_code == 0, r.output
    assert "alice" in r.output

    r = runner.invoke(main, ["--config", str(cfg), "remove-api-user", "alice"], env=env)
    assert r.exit_code == 0, r.output

    r = runner.invoke(main, ["--config", str(cfg), "list-api-users"], env=env)
    assert "alice" not in r.output


def test_add_duplicate_fails(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    r1 = runner.invoke(main, ["--config", str(cfg), "add-api-user", "bob", "--password", "pw1"], env=env)
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(main, ["--config", str(cfg), "add-api-user", "bob", "--password", "pw2"], env=env)
    assert r2.exit_code != 0


def test_add_api_user_password_stdin(db_conn, db_dsn: str, tmp_path: Path) -> None:
    """--password-stdin is the supported CI/scripts entrypoint.

    Without this, scripts had to rely on `--password` on the argv (visible in
    `ps`) or `click.prompt` (silently hangs in non-TTY automation). The
    new flag refuses to read from a TTY so an interactive operator can't
    accidentally bake an empty password on a forgotten EOF.
    """
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    r = runner.invoke(
        main,
        ["--config", str(cfg), "add-api-user", "carol", "--password-stdin"],
        env=env,
        input="hunter3\n",
    )
    assert r.exit_code == 0, r.output
    r2 = runner.invoke(main, ["--config", str(cfg), "list-api-users"], env=env)
    assert "carol" in r2.output


def test_add_api_user_non_tty_without_password_fails_fast(
    db_conn, db_dsn: str, tmp_path: Path,
) -> None:
    """No `--password`, no `--password-stdin`, no TTY → exit non-zero.

    The previous behaviour silently accepted the empty stdin stream and stored
    an empty-string password. Now it must fail clearly.
    """
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    r = runner.invoke(
        main,
        ["--config", str(cfg), "add-api-user", "dave"],
        env=env,
        input="",
    )
    assert r.exit_code != 0
