# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Heartbeat writer: upsert on both partial-index targets; clear-all."""
from __future__ import annotations

from typing import cast

import psycopg
from psycopg_pool import ConnectionPool

from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import create_account
from localmail.config import AccountConfig
from localmail.heartbeat import (
    clear_account_heartbeats,
    clear_all_heartbeats,
    record_heartbeat,
)


def _account(conn: psycopg.Connection, name: str = "acct") -> int:
    cfg = AccountConfig(
        name=name, email=f"{name}@example.com",
        imap_host="imap.example.com", imap_port=993, auth_method="password",
    )
    return create_account(conn, **account_create_kwargs(cfg)).id


def _rows(conn: psycopg.Connection) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT worker_kind, account_id, state, current_folder, last_error_msg "
            "FROM daemon_heartbeats ORDER BY account_id NULLS LAST, worker_kind"
        )
        return cur.fetchall()


def test_account_heartbeat_insert_then_update_same_row(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="connecting")
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="idle",
                     current_folder="INBOX")
    db_conn.commit()
    rows = _rows(db_conn)
    assert rows == [("idle", aid, "idle", "INBOX", None)]  # one row, updated in place


def test_two_account_threads_are_distinct_rows(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="idle")
    record_heartbeat(db_conn, worker_kind="poll", account_id=aid, state="polling")
    db_conn.commit()
    rows = _rows(db_conn)
    assert {(k, a) for k, a, *_ in rows} == {("idle", aid), ("poll", aid)}


def test_process_heartbeat_insert_then_update_same_row(db_conn: psycopg.Connection) -> None:
    record_heartbeat(db_conn, worker_kind="embed", account_id=None, state="idle")
    record_heartbeat(db_conn, worker_kind="embed", account_id=None, state="error",
                     last_error_msg="boom")
    db_conn.commit()
    rows = _rows(db_conn)
    assert rows == [("embed", None, "error", None, "boom")]  # one row, updated


def test_started_at_is_preserved_across_updates(db_conn: psycopg.Connection) -> None:
    record_heartbeat(db_conn, worker_kind="reconcile", account_id=None, state="idle")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT started_at FROM daemon_heartbeats WHERE worker_kind='reconcile'")
        row = cur.fetchone()
        assert row is not None
        first = row[0]
    record_heartbeat(db_conn, worker_kind="reconcile", account_id=None, state="idle")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT started_at, last_heartbeat_at FROM daemon_heartbeats "
                    "WHERE worker_kind='reconcile'")
        row = cur.fetchone()
        assert row is not None
        started_at, last_hb = row
    assert started_at == first  # started_at frozen on first insert
    assert last_hb >= started_at  # last_heartbeat_at advances


def test_clear_all_heartbeats_empties_table(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="idle")
    record_heartbeat(db_conn, worker_kind="embed", account_id=None, state="idle")
    db_conn.commit()
    clear_all_heartbeats(db_conn)
    db_conn.commit()
    assert _rows(db_conn) == []


def test_clear_account_heartbeats_only_removes_that_account(
    db_conn: psycopg.Connection,
) -> None:
    a1 = _account(db_conn, "acct1")
    a2 = _account(db_conn, "acct2")
    record_heartbeat(db_conn, worker_kind="idle", account_id=a1, state="idle")
    record_heartbeat(db_conn, worker_kind="poll", account_id=a1, state="polling")
    record_heartbeat(db_conn, worker_kind="idle", account_id=a2, state="idle")
    record_heartbeat(db_conn, worker_kind="embed", account_id=None, state="idle")
    db_conn.commit()
    clear_account_heartbeats(db_conn, a1)
    db_conn.commit()
    rows = _rows(db_conn)
    # a1's idle + poll rows gone; a2's account row and the process row remain.
    assert {(k, a) for k, a, *_ in rows} == {("idle", a2), ("embed", None)}


def test_safe_heartbeat_swallows_pool_errors(caplog) -> None:
    import logging

    from localmail.heartbeat import safe_heartbeat

    class _BoomPool:
        def connection(self):
            raise RuntimeError("pool exhausted")

    # Must not raise — a heartbeat failure can't be allowed to kill the loop.
    with caplog.at_level(logging.WARNING, logger="localmail.heartbeat"):
        safe_heartbeat(cast(ConnectionPool, _BoomPool()), worker_kind="idle",
                       account_id=1, state="idle")
    assert "heartbeat write failed" in caplog.text


def test_same_kind_different_accounts_are_distinct_rows(db_conn: psycopg.Connection) -> None:
    a1 = _account(db_conn, "acct1")
    a2 = _account(db_conn, "acct2")
    record_heartbeat(db_conn, worker_kind="idle", account_id=a1, state="idle")
    record_heartbeat(db_conn, worker_kind="idle", account_id=a2, state="idle")
    db_conn.commit()
    rows = _rows(db_conn)
    assert {(k, a) for k, a, *_ in rows} == {("idle", a1), ("idle", a2)}
