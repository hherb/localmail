"""Migration 0023 adds daemon_heartbeats + two partial unique indexes."""
from __future__ import annotations

import psycopg


def test_daemon_heartbeats_table_shape(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'daemon_heartbeats' "
            "ORDER BY ordinal_position"
        )
        cols = {name: (dtype, nullable) for name, dtype, nullable in cur.fetchall()}
    assert cols, "daemon_heartbeats table missing"
    assert cols["worker_kind"] == ("text", "NO")
    assert cols["account_id"] == ("bigint", "YES")
    assert cols["state"] == ("text", "NO")
    assert cols["current_folder"] == ("text", "YES")
    assert cols["last_error_msg"] == ("text", "YES")
    assert cols["started_at"] == ("timestamp with time zone", "NO")
    assert cols["last_heartbeat_at"] == ("timestamp with time zone", "NO")


def test_partial_unique_indexes_exist(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE tablename = 'daemon_heartbeats'"
        )
        defs = {name: ddl for name, ddl in cur.fetchall()}
    acct = defs.get("daemon_heartbeats_acct_idx")
    proc = defs.get("daemon_heartbeats_proc_idx")
    assert acct is not None and "UNIQUE" in acct
    assert "worker_kind" in acct and "account_id" in acct
    assert "account_id IS NOT NULL" in acct
    assert proc is not None and "UNIQUE" in proc
    assert "account_id IS NULL" in proc


def test_worker_kind_check_rejects_unknown(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO daemon_heartbeats "
                "(worker_kind, account_id, state, started_at, last_heartbeat_at) "
                "VALUES ('bogus', NULL, 'idle', now(), now())"
            )
            raised = False
        except psycopg.errors.CheckViolation:
            raised = True
        db_conn.rollback()
    assert raised, "worker_kind CHECK did not reject unknown value"


def test_state_check_rejects_unknown(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO daemon_heartbeats "
                "(worker_kind, account_id, state, started_at, last_heartbeat_at) "
                "VALUES ('idle', NULL, 'bogus', now(), now())"
            )
            raised = False
        except psycopg.errors.CheckViolation:
            raised = True
        db_conn.rollback()
    assert raised, "state CHECK did not reject unknown value"
