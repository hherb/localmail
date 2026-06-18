# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""daemon_commands service layer: enqueue (+NOTIFY), claim (SKIP LOCKED), mark;
plus the migration's CHECK constraints."""
from __future__ import annotations

import psycopg
import pytest

from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import create_account
from localmail.api.admin.daemon import (
    claim_commands,
    enqueue_command,
    mark_command,
)
from localmail.config import AccountConfig


def _account(conn: psycopg.Connection, name: str = "acct") -> int:
    cfg = AccountConfig(
        name=name, email=f"{name}@example.com",
        imap_host="imap.example.com", imap_port=993, auth_method="password",
    )
    return create_account(conn, **account_create_kwargs(cfg)).id


def _states(conn: psycopg.Connection) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT command, account_id, state FROM daemon_commands ORDER BY id"
        )
        return cur.fetchall()


def test_enqueue_reload_now_returns_id_and_queues(db_conn: psycopg.Connection) -> None:
    cmd_id = enqueue_command(db_conn, command="reload-now", requested_by=None)
    db_conn.commit()
    assert isinstance(cmd_id, int)
    assert _states(db_conn) == [("reload-now", None, "queued")]


def test_enqueue_restart_account_carries_account_id(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    enqueue_command(db_conn, command="restart-account", account_id=aid, requested_by=None)
    db_conn.commit()
    assert _states(db_conn) == [("restart-account", aid, "queued")]


def test_restart_account_requires_account_id(db_conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "INSERT INTO daemon_commands (command, account_id) VALUES ('restart-account', NULL)"
        )
    db_conn.rollback()


def test_non_restart_command_forbids_account_id(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "INSERT INTO daemon_commands (command, account_id) VALUES ('reload-now', %s)",
            (aid,),
        )
    db_conn.rollback()


def test_enqueue_emits_notify(db_conn: psycopg.Connection, db_dsn: str) -> None:
    listener = psycopg.connect(db_dsn, autocommit=True)
    try:
        listener.execute("LISTEN daemon_commands")
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()  # NOTIFY is delivered on the enqueuer's COMMIT
        got = next(listener.notifies(timeout=5, stop_after=1), None)
        assert got is not None
        assert got.channel == "daemon_commands"
    finally:
        listener.close()


def test_claim_returns_queued_oldest_first_then_mark_done(db_conn: psycopg.Connection) -> None:
    first = enqueue_command(db_conn, command="reload-now", requested_by=None)
    second = enqueue_command(db_conn, command="drain-stop", requested_by=None)
    db_conn.commit()
    claimed = claim_commands(db_conn)
    assert [c.id for c in claimed] == [first, second]
    assert claimed[0].command == "reload-now"
    for c in claimed:
        mark_command(db_conn, c.id, state="done", result_msg="ok")
    db_conn.commit()
    assert {s for _, _, s in _states(db_conn)} == {"done"}


def test_claim_skips_rows_locked_by_another_tx(db_conn: psycopg.Connection, db_dsn: str) -> None:
    enqueue_command(db_conn, command="reload-now", requested_by=None)
    db_conn.commit()
    holder = psycopg.connect(db_dsn, autocommit=False)
    try:
        held = claim_commands(holder)
        assert len(held) == 1
        assert claim_commands(db_conn) == []
    finally:
        holder.rollback()
        holder.close()


def test_mark_failed_records_result(db_conn: psycopg.Connection) -> None:
    cid = enqueue_command(db_conn, command="reload-now", requested_by=None)
    db_conn.commit()
    mark_command(db_conn, cid, state="failed", result_msg="boom")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT state, result_msg, done_at FROM daemon_commands WHERE id = %s",
            (cid,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "failed"
        assert row[1] == "boom"
        assert row[2] is not None
