# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Migration 0021 adds api_users.is_admin and a partial index on it."""
from __future__ import annotations

import psycopg


def test_is_admin_column_exists(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'api_users' AND column_name = 'is_admin'"
        )
        row = cur.fetchone()
    assert row is not None, "is_admin column missing from api_users"
    name, dtype, nullable, default = row
    assert dtype == "boolean"
    assert nullable == "NO"
    assert default == "false"


def test_partial_index_on_is_admin(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'api_users' AND indexname = 'api_users_is_admin_idx'"
        )
        row = cur.fetchone()
    assert row is not None, "api_users_is_admin_idx missing"
    indexdef = row[0]
    assert "WHERE" in indexdef and "is_admin" in indexdef
