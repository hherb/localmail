# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""CLI tests for grant-account / revoke-account / list-api-users --with-grants."""
import os
from pathlib import Path

import psycopg
from click.testing import CliRunner

from localmail.cli import main


def _make_min_config(tmp_path: Path, dsn: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[database]\ndsn = "{dsn}"\n\n[attachments]\nroot = "{tmp_path / "att"}"\n'
    )
    return cfg


def _seed_account(db_conn: psycopg.Connection, name: str) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, 'imap.x', 'password') RETURNING id",
            (name, f"{name}@x"),
        )
        row = cur.fetchone()
        assert row is not None
    db_conn.commit()
    return int(row[0])


def _run(runner: CliRunner, cfg: Path, env: dict, *args: str):
    return runner.invoke(main, ["--config", str(cfg), *args], env=env)


def test_grant_account_succeeds(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    _seed_account(db_conn, "gmail")
    _run(runner, cfg, env, "add-api-user", "alice", "--password", "hunter2")
    r = _run(runner, cfg, env, "grant-account", "alice", "gmail")
    assert r.exit_code == 0, r.output
    assert "granted" in r.output


def test_grant_account_idempotent(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    _seed_account(db_conn, "gmail")
    _run(runner, cfg, env, "add-api-user", "alice", "--password", "hunter2")
    _run(runner, cfg, env, "grant-account", "alice", "gmail")
    r2 = _run(runner, cfg, env, "grant-account", "alice", "gmail")
    assert r2.exit_code == 0, r2.output
    assert "already had access" in r2.output


def test_grant_account_unknown_user_fails(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    _seed_account(db_conn, "gmail")
    r = _run(runner, cfg, env, "grant-account", "ghost", "gmail")
    assert r.exit_code != 0
    assert "no such user" in r.output


def test_grant_account_unknown_account_fails(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    _run(runner, cfg, env, "add-api-user", "alice", "--password", "hunter2")
    r = _run(runner, cfg, env, "grant-account", "alice", "no-such-acct")
    assert r.exit_code != 0
    assert "no such account" in r.output


def test_revoke_account_removes_grant(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    _seed_account(db_conn, "gmail")
    _run(runner, cfg, env, "add-api-user", "alice", "--password", "hunter2")
    _run(runner, cfg, env, "grant-account", "alice", "gmail")
    r = _run(runner, cfg, env, "revoke-account", "alice", "gmail")
    assert r.exit_code == 0, r.output
    assert "revoked" in r.output


def test_revoke_account_when_no_grant_says_no_change(
    db_conn, db_dsn: str, tmp_path: Path,
) -> None:
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    _seed_account(db_conn, "gmail")
    _run(runner, cfg, env, "add-api-user", "alice", "--password", "hunter2")
    r = _run(runner, cfg, env, "revoke-account", "alice", "gmail")
    assert r.exit_code == 0
    assert "no change" in r.output


def test_list_api_users_with_grants(db_conn, db_dsn: str, tmp_path: Path) -> None:
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    _seed_account(db_conn, "gmail")
    _seed_account(db_conn, "proton")
    _run(runner, cfg, env, "add-api-user", "alice", "--password", "hunter2")
    _run(runner, cfg, env, "grant-account", "alice", "gmail")
    _run(runner, cfg, env, "grant-account", "alice", "proton")
    r = _run(runner, cfg, env, "list-api-users", "--with-grants")
    assert r.exit_code == 0, r.output
    assert "alice" in r.output
    assert "gmail" in r.output
    assert "proton" in r.output


def test_list_api_users_with_grants_no_grants(
    db_conn, db_dsn: str, tmp_path: Path,
) -> None:
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    _run(runner, cfg, env, "add-api-user", "alice", "--password", "hunter2")
    r = _run(runner, cfg, env, "list-api-users", "--with-grants")
    assert r.exit_code == 0, r.output
    assert "alice" in r.output
    assert "(no grants)" in r.output


def test_add_api_user_no_longer_warns_about_acl(
    db_conn, db_dsn: str, tmp_path: Path,
) -> None:
    """Pre-PR add-api-user warned when a second user was created. That
    warning is now obsolete (the ACL exists) and must not appear."""
    cfg = _make_min_config(tmp_path, db_dsn)
    env = {**os.environ, "LOCALMAIL_DSN_OVERRIDE": db_dsn}
    runner = CliRunner()
    _run(runner, cfg, env, "add-api-user", "alice", "--password", "hunter2")
    r = _run(runner, cfg, env, "add-api-user", "bob", "--password", "hunter3")
    assert r.exit_code == 0, r.output
    assert "can read every account's mail" not in r.output
    # The "use grant-account" hint should appear instead.
    assert "grant-account" in r.output
