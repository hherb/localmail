# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Schema + busy-guard tests for import_jobs (migration 0026)."""
from __future__ import annotations

import psycopg
import pytest


def _archive_account(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts "
            "  (name, email_address, auth_method, imap_host, imap_port, config) "
            "VALUES ('arch', 'a@b.test', 'archive', NULL, NULL, '{}') RETURNING id"
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _insert_job(conn, account_id, status="pending"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
            "VALUES (%s, 'mbox', '/x', %s) RETURNING id",
            (account_id, status),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def test_import_jobs_table_exists_with_defaults(db_conn):
    aid = _archive_account(db_conn)
    jid = _insert_job(db_conn, aid)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT processed, inserted, skipped_dup, failed, cancel_requested "
            "FROM import_jobs WHERE id = %s",
            (jid,),
        )
        row = cur.fetchone()
    assert row == (0, 0, 0, 0, False)


def test_busy_guard_rejects_second_active_job(db_conn):
    aid = _archive_account(db_conn)
    _insert_job(db_conn, aid, status="pending")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_job(db_conn, aid, status="running")


def test_busy_guard_allows_active_after_terminal(db_conn):
    aid = _archive_account(db_conn)
    jid = _insert_job(db_conn, aid, status="running")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE import_jobs SET status='completed' WHERE id=%s", (jid,))
    # A new active job is now permitted.
    _insert_job(db_conn, aid, status="pending")


def test_import_jobs_has_owner_columns(db_conn):
    aid = _archive_account(db_conn)
    jid = _insert_job(db_conn, aid)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs SET owner_host = 'h', owner_pid = 42 WHERE id = %s",
            (jid,),
        )
        cur.execute(
            "SELECT owner_host, owner_pid FROM import_jobs WHERE id = %s", (jid,)
        )
        row = cur.fetchone()
    assert row == ("h", 42)
