"""Background worker: fill embeddings for message_chunks where missing.

Phase 1 handles message_chunks only; attachment_chunks come in Phase 2.
The worker is account-agnostic — one instance per process, since embedding
throughput is backend-bound rather than IMAP-bound.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback

import psycopg

from localmail.config import SearchConfig
from localmail.search.chunking import MessageRow, chunk_message
from localmail.search.embeddings import EmbeddingBackend

log = logging.getLogger("localmail.search.embed_worker")


def record_failed_embedding(cur, chunk_table: str, chunk_id: int, exc: Exception) -> None:
    """Upsert a failed_embeddings row, incrementing retry_count on conflict."""
    cur.execute(
        """
        INSERT INTO failed_embeddings (chunk_table, chunk_id, error_class,
                                       error_message, error_traceback)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (chunk_table, chunk_id) DO UPDATE
        SET error_class = EXCLUDED.error_class,
            error_message = EXCLUDED.error_message,
            error_traceback = EXCLUDED.error_traceback,
            retry_count = failed_embeddings.retry_count + 1,
            last_retry_at = now()
        """,
        (
            chunk_table,
            chunk_id,
            type(exc).__name__,
            str(exc),
            traceback.format_exc(),
        ),
    )


def _chunk_messages_lazily(conn: psycopg.Connection, cfg: SearchConfig, batch: int) -> int:
    """Find messages with no chunks; chunk them; INSERT. Returns # chunked."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.subject, m.from_addr, m.from_name, m.to_addrs,
                   m.date_sent, m.body_text
            FROM messages m
            LEFT JOIN message_chunks mc ON mc.message_id = m.id
            WHERE mc.id IS NULL
            ORDER BY m.id
            LIMIT %s
            FOR UPDATE OF m SKIP LOCKED
            """,
            (batch,),
        )
        rows = cur.fetchall()
        if not rows:
            return 0
        for mid, subj, fa, fn, to, ds, body in rows:
            msg = MessageRow(id=mid, subject=subj, from_addr=fa, from_name=fn,
                             to_addrs=to, date_sent=ds, body_text=body)
            for spec in chunk_message(msg, cfg):
                cur.execute(
                    "INSERT INTO message_chunks (message_id, kind, chunk_idx, text,"
                    " token_count) VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (message_id, kind, chunk_idx) DO NOTHING",
                    (mid, spec.kind, spec.chunk_idx, spec.text, spec.token_count),
                )
    conn.commit()
    return len(rows)


def _claim_unembedded(cur, cfg: SearchConfig) -> list[tuple[int, str]]:
    cur.execute(
        """
        SELECT mc.id, mc.text FROM message_chunks mc
        WHERE mc.embedding_v1 IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM failed_embeddings fe
              WHERE fe.chunk_table = 'message_chunks' AND fe.chunk_id = mc.id
                AND fe.retry_count >= %s
          )
        ORDER BY mc.id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (cfg.embed_worker_max_chunk_retries, cfg.embed_worker_batch_size),
    )
    return cur.fetchall()


def _embed_and_store(conn, cfg, backend, claimed):
    """Embed claimed chunks; UPDATE per chunk inside a SAVEPOINT for poison isolation."""
    texts = [t for _, t in claimed]
    vectors = backend.embed_documents(texts)
    written = 0
    for (cid, _text), vec in zip(claimed, vectors, strict=True):
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT chunk")
            try:
                cur.execute(
                    "UPDATE message_chunks SET embedding_v1 = %s::halfvec,"
                    " embedded_at = now() WHERE id = %s",
                    (vec, cid),
                )
                cur.execute("RELEASE SAVEPOINT chunk")
                written += 1
            except Exception as exc:  # noqa: BLE001 — poison-pill isolation
                cur.execute("ROLLBACK TO SAVEPOINT chunk")
                record_failed_embedding(cur, "message_chunks", cid, exc)
    conn.commit()
    return written


def run_embed_worker_once(
    conn: psycopg.Connection,
    cfg: SearchConfig,
    backend: EmbeddingBackend,
) -> int:
    """One sweep: chunk pending messages, then embed pending chunks.

    Returns number of chunks newly embedded in this sweep. Used both by
    the background daemon thread and the `localmail embed-backfill` CLI.
    """
    _chunk_messages_lazily(conn, cfg, batch=max(cfg.embed_worker_batch_size, 50))
    with conn.cursor() as cur:
        claimed = _claim_unembedded(cur, cfg)
    if not claimed:
        conn.commit()
        return 0
    try:
        return _embed_and_store(conn, cfg, backend, claimed)
    except Exception as exc:  # noqa: BLE001 — batch-level fallback
        log.warning("embed_worker batch failed: %s", exc, exc_info=True)
        # Mark every claimed chunk as failed so they're not re-claimed forever
        with conn.cursor() as cur:
            for cid, _ in claimed:
                record_failed_embedding(cur, "message_chunks", cid, exc)
        conn.commit()
        return 0


def run_embed_worker(
    stop: threading.Event,
    pool,
    cfg: SearchConfig,
    backend: EmbeddingBackend,
) -> None:
    """Background loop: sleep, sweep, sleep. Exits when `stop` is set.

    Re-acquires a fresh connection from the pool each sweep to keep the
    pool's idle-rotation healthy. Backoff on consecutive empty sweeps so
    an empty queue doesn't busy-poll.
    """
    consecutive_empty = 0
    while not stop.is_set():
        try:
            with pool.connection() as conn:
                wrote = run_embed_worker_once(conn, cfg, backend)
        except Exception as exc:  # noqa: BLE001
            log.error("embed_worker sweep error: %s", exc, exc_info=True)
            wrote = 0
        if wrote == 0:
            consecutive_empty = min(consecutive_empty + 1, 6)
        else:
            consecutive_empty = 0
        sleep_s = cfg.embed_worker_poll_interval_s * (1 + consecutive_empty)
        stop.wait(timeout=sleep_s)
