"""Heartbeat wiring: reconcile writes a heartbeat; startup clears stale rows."""
from __future__ import annotations

import threading

import psycopg

from localmail.config import LocalmailConfig
from localmail.daemon import Daemon
from localmail.heartbeat import record_heartbeat


def _cfg(db_dsn: str) -> LocalmailConfig:
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    return cfg


def _heartbeat_kinds(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT worker_kind FROM daemon_heartbeats")
            return {r[0] for r in cur.fetchall()}


def _truncate(dsn: str) -> None:
    # db_dsn only applies migrations (session-scoped); it never truncates, so
    # leftover `accounts` rows from earlier tests would make start_workers /
    # reconcile spawn IMAP threads. Clear both tables for a deterministic run.
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE accounts, daemon_heartbeats RESTART IDENTITY CASCADE"
        )
        conn.commit()


def test_reconcile_records_reconcile_heartbeat(db_dsn: str) -> None:
    _truncate(db_dsn)
    d = Daemon(_cfg(db_dsn), ssl=False, stop_event=threading.Event())
    try:
        d.reconcile()
        assert "reconcile" in _heartbeat_kinds(db_dsn)
    finally:
        d.stop()
        d.pool.close()


def test_startup_clears_leftover_heartbeats(db_dsn: str) -> None:
    _truncate(db_dsn)
    with psycopg.connect(db_dsn) as conn:
        record_heartbeat(conn, worker_kind="embed", account_id=None, state="idle")
        conn.commit()
    d = Daemon(_cfg(db_dsn), ssl=False, stop_event=threading.Event())
    try:
        d.start_workers()  # spawns no account threads (no syncable accounts), clears HBs
        assert "embed" not in _heartbeat_kinds(db_dsn)
    finally:
        d.stop()
        d.pool.close()
