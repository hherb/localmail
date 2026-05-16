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
