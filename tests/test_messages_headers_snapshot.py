"""#383: ``messages.headers`` is a sync-time snapshot, kept; its GIN index is not.

Since #379 no query reads the column (the read path parses ``raw_bytes``), so
``messages_headers_gin`` (0001) cost every INSERT an index update and served
nothing; on the Mac archive it was 355 MB, larger than the table's heap.
Migration 0039 drops it. The column stays: it records what the parser saw at
sync time, which a fresh parse of ``raw_bytes`` does not reproduce.
"""
from __future__ import annotations

import psycopg


def test_0039_dropped_the_headers_gin_index(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_indexes WHERE tablename = 'messages'"
                    " AND indexname = 'messages_headers_gin'")
        assert cur.fetchone() is None


def test_the_headers_column_is_kept(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT is_nullable FROM information_schema.columns"
                    " WHERE table_name = 'messages' AND column_name = 'headers'")
        assert cur.fetchone() == ("NO",)
