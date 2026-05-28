# tests/test_migration_0020.py
"""Regression tests for the 0020_accounts_canonical migration.

Exercises the post-migration shape against the real test DB (the migration
has already applied via the conftest db_conn fixture, which TRUNCATEs).
"""

from __future__ import annotations

import psycopg
import pytest


def test_folder_filter_columns_exist(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT column_name
              FROM information_schema.columns
             WHERE table_name = 'accounts'
               AND column_name IN ('folder_allow', 'folder_deny',
                                   'folder_deny_flags', 'sync_enabled',
                                   'updated_at')
        """)
        present = {row[0] for row in cur.fetchall()}
    assert present == {
        'folder_allow', 'folder_deny', 'folder_deny_flags',
        'sync_enabled', 'updated_at',
    }


def test_archive_auth_method_is_accepted(db_conn: psycopg.Connection) -> None:
    # imap_port has a column default of 993, so archive accounts must set it
    # explicitly to NULL to satisfy the accounts_live_requires_host constraint
    # (which requires imap_host IS NULL AND imap_port IS NULL for 'archive').
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port) "
            "VALUES ('arch', 'a@b.test', 'archive', NULL, NULL) RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        inserted_id = row[0]

        cur.execute(
            "SELECT auth_method FROM accounts WHERE id = %s", (inserted_id,)
        )
        auth_row = cur.fetchone()
        assert auth_row is not None
        assert auth_row[0] == "archive"


def test_archive_accounts_cannot_have_host(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, "
            "imap_host, imap_port) "
            "VALUES ('arch2', 'a@b.test', 'archive', 'imap.example', 993)"
        )


def test_live_accounts_must_have_host(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method) "
            "VALUES ('broken', 'a@b.test', 'password')"
        )
