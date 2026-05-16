"""Integration tests for the embed worker against a real Postgres."""

from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.search.embed_worker import (
    record_failed_embedding,
    run_embed_worker_once,
)


def _seed_message(conn, body="Hello world."):
    """Insert an account + message; return message_id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('a', 'a@x', 'h', 'password') RETURNING id"
        )
        acct = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " from_addr, body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, %s) RETURNING id",
            (acct, "<a@x>", b'\\x01' * 32, "Hi", "x@y", body, b"raw", 3),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


class _StaticEmbedder:
    """Deterministic backend: returns [1.0]*768 for any text."""
    name = "stub"
    model = "stub-768"
    dimension = 768

    def embed_documents(self, texts):
        return [[1.0 / (i + 1)] * 768 for i, _ in enumerate(texts)]

    def embed_query(self, text):
        return [0.5] * 768

    def health_check(self):
        pass


def test_run_embed_worker_chunks_and_embeds_a_message(db_conn):
    mid = _seed_message(db_conn, body="The Berlin conference is next week.")
    cfg = SearchConfig(embed_worker_batch_size=10)
    embedded = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert embedded >= 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM message_chunks WHERE message_id = %s"
            " AND embedding_v1 IS NOT NULL", (mid,))
        assert cur.fetchone()[0] >= 1


def test_run_embed_worker_idempotent(db_conn):
    mid = _seed_message(db_conn)
    cfg = SearchConfig()
    first = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    second = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert first >= 1 and second == 0


def test_record_failed_embedding_inserts_row(db_conn):
    mid = _seed_message(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
            " VALUES (%s, 'body', 0, 'x', 1) RETURNING id", (mid,))
        cid = cur.fetchone()[0]
    db_conn.commit()
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        record_failed_embedding(db_conn, "message_chunks", cid, exc)
        db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT error_class, error_message, retry_count FROM failed_embeddings"
            " WHERE chunk_table='message_chunks' AND chunk_id=%s", (cid,))
        row = cur.fetchone()
    assert row[0] == "RuntimeError"
    assert "boom" in row[1]
    assert row[2] == 0


def test_record_failed_embedding_bumps_retry_count(db_conn):
    mid = _seed_message(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
            " VALUES (%s, 'body', 0, 'x', 1) RETURNING id", (mid,))
        cid = cur.fetchone()[0]
    db_conn.commit()
    for _ in range(3):
        try:
            raise ValueError("again")
        except ValueError as exc:
            record_failed_embedding(db_conn, "message_chunks", cid, exc)
            db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT retry_count FROM failed_embeddings WHERE chunk_id=%s", (cid,))
        assert cur.fetchone()[0] == 2  # 0 on insert, +1 each subsequent


def test_claim_filter_skips_chunks_past_max_retries(db_conn):
    """Chunks with retry_count >= max are excluded from claim selection."""
    mid = _seed_message(db_conn, body="x")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO message_chunks (message_id, kind, chunk_idx, text, token_count)"
            " VALUES (%s, 'body', 0, 'x', 1) RETURNING id", (mid,))
        cid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO failed_embeddings (chunk_table, chunk_id, error_class,"
            " error_message, retry_count) VALUES ('message_chunks', %s, 'X', 'X', 5)",
            (cid,),
        )
    db_conn.commit()

    cfg = SearchConfig(embed_worker_max_chunk_retries=3)
    embedded = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert embedded == 0


def test_batch_failure_does_not_poison_queue(db_conn):
    """A transient backend error rolls back without marking chunks failed."""
    _seed_message(db_conn, body="hello world")

    class _Boom:
        name = "boom"; model = "boom"; dimension = 768
        def embed_documents(self, texts): raise RuntimeError("transient blip")
        def embed_query(self, t): return [0.0] * 768
        def health_check(self): pass

    cfg = SearchConfig(embed_worker_max_chunk_retries=3)
    # First sweep chunks the message but the batch embedding raises.
    embedded = run_embed_worker_once(db_conn, cfg, _Boom())
    assert embedded == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM failed_embeddings")
        assert cur.fetchone()[0] == 0

    # A subsequent sweep with a working backend embeds the same chunks.
    embedded = run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert embedded >= 1


def test_chunking_failure_records_failed_chunking_and_skips_message(db_conn, monkeypatch):
    """A poison message lands in failed_chunkings and is excluded next sweep."""
    from localmail.search import embed_worker as ew

    mid = _seed_message(db_conn, body="poisonous content")

    real_chunk_message = ew.chunk_message
    calls = {"n": 0}

    def boom_chunk_message(msg, cfg):
        calls["n"] += 1
        if msg.id == mid:
            raise RuntimeError("cannot chunk this message")
        return real_chunk_message(msg, cfg)

    monkeypatch.setattr(ew, "chunk_message", boom_chunk_message)

    cfg = SearchConfig(embed_worker_max_chunk_retries=1)
    # Sweep 1: failure recorded with retry_count = 0.
    run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    # Sweep 2: row still selectable (0 < 1); retry bumps to 1.
    run_embed_worker_once(db_conn, cfg, _StaticEmbedder())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT error_class, retry_count FROM failed_chunkings WHERE message_id=%s",
            (mid,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "RuntimeError"
    assert row[1] == 1

    calls_before = calls["n"]
    # Sweep 3: 1 >= 1 → message excluded, chunk_message not invoked again.
    run_embed_worker_once(db_conn, cfg, _StaticEmbedder())
    assert calls["n"] == calls_before
