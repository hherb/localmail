# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""CLI commands for admin grant/revoke and add-api-user --admin."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from click.testing import CliRunner

from localmail.cli import main


def _is_admin(conn: psycopg.Connection, username: str) -> bool | None:
    with conn.cursor() as cur:
        cur.execute("SELECT is_admin FROM api_users WHERE username = %s", (username,))
        row = cur.fetchone()
    return None if row is None else bool(row[0])


def _make_cfg(tmp_path: Path, dsn: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[database]\ndsn = "{dsn}"\n\n[attachments]\nroot = "{tmp_path / "att"}"\n'
    )
    return cfg


def _env(db_dsn: str) -> dict[str, str]:
    return {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}


def test_grant_admin_promotes_existing_user(db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, db_dsn)
    env = _env(db_dsn)

    runner = CliRunner()
    res = runner.invoke(main, ["--config", str(cfg), "add-api-user", "--password", "hunter2", "horst"], env=env)
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is False

    res = runner.invoke(main, ["--config", str(cfg), "grant-admin", "horst"], env=env)
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is True


def test_revoke_admin_demotes(db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, db_dsn)
    env = _env(db_dsn)

    runner = CliRunner()
    res = runner.invoke(main, ["--config", str(cfg), "add-api-user", "--admin", "--password", "hunter2", "horst"], env=env)
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is True

    res = runner.invoke(main, ["--config", str(cfg), "revoke-admin", "horst"], env=env)
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is False


def test_grant_admin_unknown_user_errors(db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, db_dsn)
    env = _env(db_dsn)

    runner = CliRunner()
    res = runner.invoke(main, ["--config", str(cfg), "grant-admin", "ghost"], env=env)
    assert res.exit_code != 0
    assert "no user named 'ghost'" in res.output


def test_add_api_user_admin_flag(db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, db_dsn)
    env = _env(db_dsn)

    runner = CliRunner()
    res = runner.invoke(main, ["--config", str(cfg), "add-api-user", "--admin", "--password", "hunter2", "horst"], env=env)
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is True


def _sessions_invalidated_at(conn: psycopg.Connection, username: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sessions_invalidated_at FROM api_users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
    return None if row is None else row[0]


def test_revoke_admin_sessions_bumps_column(
    db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path
) -> None:
    cfg = _make_cfg(tmp_path, db_dsn)
    env = _env(db_dsn)

    runner = CliRunner()
    res = runner.invoke(
        main,
        ["--config", str(cfg), "add-api-user", "--admin", "--password", "hunter2", "horst"],
        env=env,
    )
    assert res.exit_code == 0, res.output
    assert _sessions_invalidated_at(db_conn, "horst") is None

    res = runner.invoke(
        main, ["--config", str(cfg), "revoke-admin-sessions", "horst"], env=env
    )
    assert res.exit_code == 0, res.output
    assert _sessions_invalidated_at(db_conn, "horst") is not None


def test_revoke_admin_sessions_unknown_user_errors(
    db_conn: psycopg.Connection, db_dsn: str, tmp_path: Path
) -> None:
    cfg = _make_cfg(tmp_path, db_dsn)
    env = _env(db_dsn)

    runner = CliRunner()
    res = runner.invoke(
        main, ["--config", str(cfg), "revoke-admin-sessions", "ghost"], env=env
    )
    assert res.exit_code != 0
    assert "no user named 'ghost'" in res.output
