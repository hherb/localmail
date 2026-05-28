"""CLI commands for admin grant/revoke and add-api-user --admin."""
from __future__ import annotations

import psycopg
from click.testing import CliRunner

from localmail.cli import main


def _is_admin(conn: psycopg.Connection, username: str) -> bool | None:
    with conn.cursor() as cur:
        cur.execute("SELECT is_admin FROM api_users WHERE username = %s", (username,))
        row = cur.fetchone()
    return None if row is None else bool(row[0])


def test_grant_admin_promotes_existing_user(db_conn: psycopg.Connection, tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\ndsn = "{db_conn.info.dsn}"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(cfg))

    runner = CliRunner()
    res = runner.invoke(main, ["add-api-user", "--password", "hunter2", "horst"])
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is False

    res = runner.invoke(main, ["grant-admin", "horst"])
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is True


def test_revoke_admin_demotes(db_conn: psycopg.Connection, tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\ndsn = "{db_conn.info.dsn}"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(cfg))

    runner = CliRunner()
    runner.invoke(main, ["add-api-user", "--admin", "--password", "hunter2", "horst"])
    assert _is_admin(db_conn, "horst") is True

    res = runner.invoke(main, ["revoke-admin", "horst"])
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is False


def test_grant_admin_unknown_user_errors(tmp_path, monkeypatch, db_conn) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\ndsn = "{db_conn.info.dsn}"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(cfg))

    runner = CliRunner()
    res = runner.invoke(main, ["grant-admin", "ghost"])
    assert res.exit_code != 0
    assert "no user named 'ghost'" in res.output


def test_add_api_user_admin_flag(db_conn: psycopg.Connection, tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\ndsn = "{db_conn.info.dsn}"\n')
    monkeypatch.setenv("LOCALMAIL_CONFIG", str(cfg))

    runner = CliRunner()
    res = runner.invoke(main, ["add-api-user", "--admin", "--password", "hunter2", "horst"])
    assert res.exit_code == 0, res.output
    assert _is_admin(db_conn, "horst") is True
