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


def test_attachment_text_and_chunks_tables_exist(db_conn) -> None:
    """Verify migration 0011 created attachment_text and attachment_chunks with correct schema.

    Checks column names, nullability, unique constraint on (sha256, chunk_idx),
    and the partial pending index on attachment_chunks.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, is_nullable, data_type "
            "FROM information_schema.columns "
            "WHERE table_name = 'attachment_text' ORDER BY ordinal_position"
        )
        cols = cur.fetchall()
    names = [c[0] for c in cols]
    assert names == ["sha256", "extractor", "extracted_text", "page_count", "extracted_at"]

    nullable = {c[0]: c[1] for c in cols}
    assert nullable["extractor"] == "NO"
    assert nullable["extracted_text"] == "NO"
    assert nullable["page_count"] == "YES"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'attachment_chunks' ORDER BY ordinal_position"
        )
        names = [r[0] for r in cur.fetchall()]
    assert names == [
        "id", "sha256", "chunk_idx", "text", "token_count", "embedding_v1", "embedded_at"
    ]

    # Unique (sha256, chunk_idx)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = 'attachment_chunks' "
            "  AND c.contype = 'u' "
            "  AND pg_get_constraintdef(c.oid) LIKE '%(sha256, chunk_idx)%'"
        )
        assert cur.fetchone() is not None

    # Partial pending index
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'attachment_chunks_pending_idx'"
        )
        row = cur.fetchone()
    assert row is not None
    assert "WHERE (embedding_v1 IS NULL)" in row[0]


def test_failed_extractions_table_exists(db_conn) -> None:
    """Verify migration 0012 created failed_extractions with correct schema.

    Checks column names in order, nullability of key columns, and that the
    primary key is sha256 alone (one row per blob, not per (blob, extractor)).
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'failed_extractions' ORDER BY ordinal_position"
        )
        cols = cur.fetchall()
    names = [c[0] for c in cols]
    assert names == [
        "sha256", "extractor", "error_class", "error_message", "traceback",
        "retry_count", "failed_at", "last_retry_at",
    ]
    nullable = {c[0]: c[1] for c in cols}
    assert nullable["extractor"] == "NO"
    assert nullable["traceback"] == "YES"
    assert nullable["last_retry_at"] == "YES"
    assert nullable["error_class"] == "NO"
    assert nullable["error_message"] == "NO"
    assert nullable["retry_count"] == "NO"

    # PK is sha256 alone
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'failed_extractions'::regclass "
            "AND i.indisprimary "
            "ORDER BY a.attnum"
        )
        pk_cols = [r[0] for r in cur.fetchall()]
    assert pk_cols == ["sha256"]


def test_failed_extractions_cascade_on_blob_delete(db_conn) -> None:
    """Deleting an attachment_blobs row cascades to failed_extractions."""
    sha = b"\x55" * 32
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/tmp/fe-cascade", "application/pdf", 1),
        )
        cur.execute(
            "INSERT INTO failed_extractions "
            "(sha256, extractor, error_class, error_message) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "lightweight", "BadFile", "broken"),
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM attachment_blobs WHERE sha256 = %s", (sha,))
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM failed_extractions WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


def test_attachment_arm4_indexes_exist(db_conn) -> None:
    """Verify migration 0013 created HNSW on attachment_chunks.embedding_v1
    and GIN on messages.attachments for Arm 4 vector + JSONB retrieval.

    The HNSW WITH-clause parameters must match Phase 1's message_chunks index
    (m=16, ef_construction=64) for consistent build cost and recall.
    Postgres 18 normalises these as quoted integers in indexdef
    (e.g. m='16'), so the assertions match that canonical form.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'attachment_chunks_embedding_v1_hnsw'"
        )
        row = cur.fetchone()
    assert row is not None, "attachment_chunks_embedding_v1_hnsw index missing"
    assert "USING hnsw" in row[0]
    assert "halfvec_cosine_ops" in row[0]
    # Postgres normalises WITH-clause params to quoted integers in indexdef.
    assert "m='16'" in row[0]
    assert "ef_construction='64'" in row[0]

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'messages_attachments_gin'"
        )
        row = cur.fetchone()
    assert row is not None, "messages_attachments_gin index missing"
    assert "USING gin" in row[0]
    # GIN target column must be `attachments`, not some other JSONB column.
    # Postgres includes the schema prefix in indexdef (e.g. "public.messages"),
    # so match the tail that is stable across schema qualification.
    assert "using gin (attachments)" in row[0].lower()


def test_messages_body_lang_pending_index_is_eligible_for_worker_query(db_conn) -> None:
    """The partial index must actually be eligible for the lang-detect
    worker's claim query — not just exist with a matching-looking predicate.

    Force the planner to prefer indexes (seqscan off) and verify EXPLAIN
    reports a scan of `messages_body_lang_pending_idx` for the exact
    `WHERE body_lang IS NULL AND body_text IS NOT NULL ORDER BY id LIMIT N
    FOR UPDATE SKIP LOCKED` shape from `run_lang_detect_pass`. An empty
    table would let the planner pick seq-scan even with the flag off, so
    seed two rows first.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('lt', 'lt@x', 'h', 'password') RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        acct_id = row[0]
        cur.executemany(
            "INSERT INTO messages (account_id, raw_sha256, headers, raw_bytes, "
            "size_bytes, body_text, body_lang) "
            "VALUES (%s, %s, '{}'::jsonb, %s, 1, %s, %s)",
            [
                (acct_id, b"\x10" * 32, b"x", "hello", None),
                (acct_id, b"\x11" * 32, b"x", "bonjour", "en"),
            ],
        )
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute(
            "EXPLAIN SELECT id, body_text FROM messages "
            "WHERE body_lang IS NULL AND body_text IS NOT NULL "
            "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        plan = "\n".join(r[0] for r in cur.fetchall())
    assert "messages_body_lang_pending_idx" in plan, plan


def test_messages_body_lang_pending_index_exists(db_conn) -> None:
    """Migration 0017 adds the partial index that backs the lang-detect
    worker's claim query (`body_lang IS NULL AND body_text IS NOT NULL`).

    The predicate must match the worker query verbatim or the planner will
    not pick the index and fall back to a seq scan on large archives.
    Postgres normalises the predicate text in `pg_indexes.indexdef` with
    parentheses and operator spacing — match the canonical form rather
    than the source SQL.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'messages_body_lang_pending_idx'"
        )
        row = cur.fetchone()
    assert row is not None, "messages_body_lang_pending_idx index missing"
    indexdef = row[0]
    assert "USING btree (id)" in indexdef
    assert "WHERE ((body_lang IS NULL) AND (body_text IS NOT NULL))" in indexdef


def test_attachment_text_and_chunks_cascade_on_blob_delete(db_conn) -> None:
    """Deleting an attachment_blobs row cascades to both attachment_text
    and attachment_chunks rows referencing the same sha256."""
    sha = b"\x42" * 32
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/tmp/cascade-test", "text/plain", 4),
        )
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, %s, %s)",
            (sha, "lightweight@1.0", "test"),
        )
        cur.execute(
            "INSERT INTO attachment_chunks "
            "(sha256, chunk_idx, text, token_count) "
            "VALUES (%s, 0, %s, 1)",
            (sha, "test"),
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM attachment_blobs WHERE sha256 = %s", (sha,))
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attachment_text WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0
        cur.execute("SELECT count(*) FROM attachment_chunks WHERE sha256 = %s", (sha,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0
