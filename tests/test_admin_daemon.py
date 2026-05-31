"""Daemon-status read accessor: staleness derivation + ordering."""
from __future__ import annotations

import psycopg

from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import create_account
from localmail.api.admin.daemon import get_daemon_status
from localmail.config import AccountConfig
from localmail.heartbeat import record_heartbeat


def _account(conn: psycopg.Connection, name: str = "acct") -> int:
    cfg = AccountConfig(
        name=name, email=f"{name}@example.com",
        imap_host="imap.example.com", imap_port=993, auth_method="password",
    )
    return create_account(conn, **account_create_kwargs(cfg)).id


def test_empty_status_when_no_heartbeats(db_conn: psycopg.Connection) -> None:
    status = get_daemon_status(db_conn, stale_seconds=120)
    assert status.heartbeats == []


def test_fresh_heartbeat_is_not_stale(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="idle")
    db_conn.commit()
    status = get_daemon_status(db_conn, stale_seconds=120)
    assert len(status.heartbeats) == 1
    hb = status.heartbeats[0]
    assert hb.worker_kind == "idle"
    assert hb.account_id == aid
    assert hb.state == "idle"
    assert hb.stale is False


def test_old_heartbeat_is_stale(db_conn: psycopg.Connection) -> None:
    record_heartbeat(db_conn, worker_kind="embed", account_id=None, state="idle")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE daemon_heartbeats "
            "SET last_heartbeat_at = now() - interval '10 minutes' "
            "WHERE worker_kind = 'embed'"
        )
    db_conn.commit()
    status = get_daemon_status(db_conn, stale_seconds=120)
    assert len(status.heartbeats) == 1
    assert status.heartbeats[0].stale is True


def test_rows_ordered_account_first_then_kind(db_conn: psycopg.Connection) -> None:
    aid = _account(db_conn)
    record_heartbeat(db_conn, worker_kind="poll", account_id=aid, state="polling")
    record_heartbeat(db_conn, worker_kind="idle", account_id=aid, state="idle")
    record_heartbeat(db_conn, worker_kind="reconcile", account_id=None, state="idle")
    db_conn.commit()
    status = get_daemon_status(db_conn, stale_seconds=120)
    kinds = [(hb.account_id, hb.worker_kind) for hb in status.heartbeats]
    assert kinds == [(aid, "idle"), (aid, "poll"), (None, "reconcile")]
