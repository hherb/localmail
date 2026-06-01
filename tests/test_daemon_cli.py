"""`localmail daemon …` CLI subgroup (2B.4).

Plane A commands (status / reload / restart-account) work against the DB and
need no running serve. Plane B commands (start / stop / restart) require the
supervisor's control socket; with `supervise_daemon = false` they exit non-zero
with the external-supervisor note, and with no serve running they exit non-zero
with an unreachable note.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from click.testing import CliRunner

from localmail.cli import main


def _make_cfg(tmp_path: Path, dsn: str, *, supervise: bool = True) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[database]\ndsn = "{dsn}"\n\n'
        f'[attachments]\nroot = "{tmp_path / "att"}"\n\n'
        f'[serve]\nsupervise_daemon = {"true" if supervise else "false"}\n'
        f'runtime_dir = "{tmp_path / "run"}"\n'
    )
    (tmp_path / "run").mkdir(exist_ok=True)
    return cfg


def _queued(conn: psycopg.Connection) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT command, account_id FROM daemon_commands "
            "WHERE state = 'queued' ORDER BY id"
        )
        return cur.fetchall()


def _seed_account(conn: psycopg.Connection, name: str = "acct") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port) VALUES (%s, 'a@example.com', 'password', "
            "'imap.example.com', 993) RETURNING id",
            (name,),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _seed_heartbeat(conn: psycopg.Connection, account_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO daemon_heartbeats (worker_kind, account_id, state, "
            "current_folder, started_at, last_heartbeat_at) VALUES "
            "('idle', %s, 'idle', 'INBOX', now(), now())",
            (account_id,),
        )
    conn.commit()


# --- Plane A: reload / restart-account ------------------------------------

def test_reload_enqueues(db_conn, db_dsn, tmp_path) -> None:
    cfg = _make_cfg(tmp_path, db_dsn)
    res = CliRunner().invoke(main, ["--config", str(cfg), "daemon", "reload"])
    assert res.exit_code == 0, res.output
    assert _queued(db_conn) == [("reload-now", None)]


def test_restart_account_enqueues(db_conn, db_dsn, tmp_path) -> None:
    aid = _seed_account(db_conn)
    cfg = _make_cfg(tmp_path, db_dsn)
    res = CliRunner().invoke(
        main, ["--config", str(cfg), "daemon", "restart-account", "acct"]
    )
    assert res.exit_code == 0, res.output
    assert _queued(db_conn) == [("restart-account", aid)]


def test_restart_account_unknown_errors(db_conn, db_dsn, tmp_path) -> None:
    cfg = _make_cfg(tmp_path, db_dsn)
    res = CliRunner().invoke(
        main, ["--config", str(cfg), "daemon", "restart-account", "ghost"]
    )
    assert res.exit_code != 0
    assert _queued(db_conn) == []


# --- status ---------------------------------------------------------------

def test_status_lists_heartbeats_external(db_conn, db_dsn, tmp_path) -> None:
    aid = _seed_account(db_conn)
    _seed_heartbeat(db_conn, aid)
    cfg = _make_cfg(tmp_path, db_dsn, supervise=False)
    res = CliRunner().invoke(main, ["--config", str(cfg), "daemon", "status"])
    assert res.exit_code == 0, res.output
    assert "idle" in res.output
    assert "external" in res.output.lower()


def test_status_when_supervised_but_no_serve(db_conn, db_dsn, tmp_path) -> None:
    aid = _seed_account(db_conn)
    _seed_heartbeat(db_conn, aid)
    cfg = _make_cfg(tmp_path, db_dsn, supervise=True)
    res = CliRunner().invoke(main, ["--config", str(cfg), "daemon", "status"])
    # Heartbeats still print; process state is unreachable but not a failure.
    assert res.exit_code == 0, res.output
    assert "idle" in res.output


# --- Plane B lifecycle gating --------------------------------------------

def test_start_external_exits_nonzero(db_conn, db_dsn, tmp_path) -> None:
    cfg = _make_cfg(tmp_path, db_dsn, supervise=False)
    res = CliRunner().invoke(main, ["--config", str(cfg), "daemon", "start"])
    assert res.exit_code != 0
    assert "external" in res.output.lower()


def test_start_no_serve_exits_nonzero(db_conn, db_dsn, tmp_path) -> None:
    cfg = _make_cfg(tmp_path, db_dsn, supervise=True)
    res = CliRunner().invoke(main, ["--config", str(cfg), "daemon", "start"])
    assert res.exit_code != 0
