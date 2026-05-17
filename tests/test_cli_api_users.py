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
