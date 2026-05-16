"""Verify search-related tables/columns/indexes exist after migration."""

from __future__ import annotations


def test_message_chunks_table_shape(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'message_chunks'
            ORDER BY ordinal_position
        """)
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert rows["id"][0] == "bigint"
    assert rows["message_id"] == ("bigint", "NO")
    assert rows["kind"] == ("text", "NO")
    assert rows["chunk_idx"] == ("integer", "NO")
    assert rows["text"] == ("text", "NO")
    assert rows["token_count"] == ("integer", "NO")
    assert rows["embedded_at"][1] == "YES"
    # halfvec shows up as USER-DEFINED — verify by name
    cur_type = rows["embedding_v1"][0]
    assert cur_type in ("USER-DEFINED", "halfvec")


def test_message_chunks_unique_constraint(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO accounts (name, email_address, imap_host, auth_method)
            VALUES ('t', 't@x', 'h', 'password') RETURNING id
        """)
        acct = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO messages (account_id, raw_sha256, headers, raw_bytes, size_bytes)
            VALUES (%s, %s, '{}'::jsonb, %s, 1) RETURNING id
        """, (acct, b"\x00" * 32, b'x'))
        msg = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
            " VALUES (%s, 'body', 0, 'hi', 1)", (msg,))
        try:
            cur.execute(
                "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
                " VALUES (%s, 'body', 0, 'hi', 1)", (msg,))
            raise AssertionError("expected unique violation")
        except Exception as exc:
            assert "unique" in str(exc).lower() or "duplicate" in str(exc).lower()


def test_failed_embeddings_table_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('failed_embeddings')")
        assert cur.fetchone()[0] == "failed_embeddings"


def test_embedding_models_table_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('embedding_models')")
        assert cur.fetchone()[0] == "embedding_models"


def test_messages_fts_v2_column_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT data_type, is_generated
            FROM information_schema.columns
            WHERE table_name = 'messages' AND column_name = 'fts_v2'
        """)
        row = cur.fetchone()
    assert row is not None, "fts_v2 column missing from messages"
    assert row[0] in ("tsvector", "USER-DEFINED")
    assert row[1] == "ALWAYS"


def test_messages_fts_v2_index_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'messages_fts_v2_idx'"
        )
        assert cur.fetchone() is not None, "messages_fts_v2_idx index missing"


def test_old_messages_fts_idx_dropped(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'messages_fts_idx'"
        )
        assert cur.fetchone() is None, "old messages_fts_idx should have been dropped"


def test_message_chunks_fts_column_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT data_type, is_generated
            FROM information_schema.columns
            WHERE table_name = 'message_chunks' AND column_name = 'fts'
        """)
        row = cur.fetchone()
    assert row is not None, "fts column missing from message_chunks"
    assert row[0] in ("tsvector", "USER-DEFINED")
    assert row[1] == "ALWAYS"


def test_message_chunks_fts_index_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'message_chunks_fts_idx'"
        )
        assert cur.fetchone() is not None, "message_chunks_fts_idx index missing"


def test_message_chunks_hnsw_index_exists(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'message_chunks_embedding_v1_hnsw'"
        )
        assert cur.fetchone() is not None, "message_chunks_embedding_v1_hnsw index missing"


def test_fts_v2_finds_subject_match(db_conn) -> None:
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO accounts (name, email_address, imap_host, auth_method)
            VALUES ('fts_test', 'fts@x', 'h', 'password') RETURNING id
        """)
        acct = cur.fetchone()
        assert acct is not None
        acct_id = acct[0]
        cur.execute("""
            INSERT INTO messages (account_id, raw_sha256, headers, raw_bytes, size_bytes,
                                  subject)
            VALUES (%s, %s, '{}'::jsonb, %s, 1, 'Conference in Berlin')
            RETURNING id
        """, (acct_id, b"\xff" * 32, b'x'))
        msg = cur.fetchone()
        assert msg is not None
        msg_id = msg[0]
        cur.execute(
            "SELECT id FROM messages WHERE id = %s"
            " AND fts_v2 @@ plainto_tsquery('simple', 'Berlin')",
            (msg_id,)
        )
        row = cur.fetchone()
    assert row is not None, "fts_v2 generated column did not match 'Berlin' in subject"
