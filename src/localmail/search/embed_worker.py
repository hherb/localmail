"""Background worker: fill embeddings for message_chunks where missing.

Phase 1 handles message_chunks only; attachment_chunks come in Phase 2.
The worker is account-agnostic — one instance per process, since embedding
throughput is backend-bound rather than IMAP-bound.

Failure model (mirrors sync.py poison-pill handling):
  - Per-message SAVEPOINT around chunk_message() + chunk INSERTs. A poison
    message lands in failed_chunkings (keyed by message_id) and is skipped
    on subsequent sweeps once retry_count >= embed_worker_max_chunk_retries.
  - Per-chunk SAVEPOINT around the embedding UPDATE. A poison chunk lands
    in failed_embeddings and is skipped likewise.
  - Both failure-recording paths use their own nested SAVEPOINT so a logging
    failure can't kill the outer transaction.
  - Batch-level errors (e.g. backend model load failure, network blip) do
    NOT mark individual chunks as failed — they roll back, log, and back off.
    The same chunks get re-claimed next sweep. Permanently-broken backends
    surface via repeated WARNINGs rather than silently poisoning the queue.
"""

from __future__ import annotations

import logging
import threading
import traceback

import psycopg

from localmail.config import SearchConfig
from localmail.search.chunking import MessageRow, chunk_message
from localmail.search.embeddings import EmbeddingBackend

log = logging.getLogger("localmail.search.embed_worker")


def _record_with_savepoint(conn, sql: str, params: tuple) -> None:
    """Run a single INSERT inside a nested SAVEPOINT so its failure can't
    abort the outer transaction."""
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT failure_log")
        try:
            cur.execute(sql, params)
            cur.execute("RELEASE SAVEPOINT failure_log")
        except Exception as log_exc:  # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT failure_log")
            log.error("failed to record failure row: %s", log_exc, exc_info=True)


_FAILED_EMBEDDING_UPSERT = """
INSERT INTO failed_embeddings (chunk_table, chunk_id, error_class,
                               error_message, error_traceback)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (chunk_table, chunk_id) DO UPDATE
SET error_class = EXCLUDED.error_class,
    error_message = EXCLUDED.error_message,
    error_traceback = EXCLUDED.error_traceback,
    retry_count = failed_embeddings.retry_count + 1,
    last_retry_at = now()
"""


_FAILED_CHUNKING_UPSERT = """
INSERT INTO failed_chunkings (message_id, error_class,
                              error_message, error_traceback)
VALUES (%s, %s, %s, %s)
ON CONFLICT (message_id) DO UPDATE
SET error_class = EXCLUDED.error_class,
    error_message = EXCLUDED.error_message,
    error_traceback = EXCLUDED.error_traceback,
    retry_count = failed_chunkings.retry_count + 1,
    last_retry_at = now()
"""


def record_failed_embedding(conn, chunk_table: str, chunk_id: int, exc: Exception) -> None:
    """Upsert a failed_embeddings row inside a nested SAVEPOINT."""
    _record_with_savepoint(
        conn,
        _FAILED_EMBEDDING_UPSERT,
        (chunk_table, chunk_id, type(exc).__name__, str(exc), traceback.format_exc()),
    )


def record_failed_chunking(conn, message_id: int, exc: Exception) -> None:
    """Upsert a failed_chunkings row inside a nested SAVEPOINT."""
    _record_with_savepoint(
        conn,
        _FAILED_CHUNKING_UPSERT,
        (message_id, type(exc).__name__, str(exc), traceback.format_exc()),
    )


def _chunk_messages_lazily(conn: psycopg.Connection, cfg: SearchConfig, batch: int) -> int:
    """Find messages with no chunks; chunk them; INSERT. Returns # processed.

    Per-message SAVEPOINT isolates poison messages so a single broken row
    only loses itself; the failure lands in failed_chunkings keyed on
    message_id, and the message is skipped on subsequent sweeps.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.subject, m.from_addr, m.from_name, m.to_addrs,
                   m.date_sent, m.body_text
            FROM messages m
            LEFT JOIN message_chunks mc ON mc.message_id = m.id
            WHERE mc.id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM failed_chunkings fc
                  WHERE fc.message_id = m.id
                    AND fc.retry_count >= %s
              )
            ORDER BY m.id
            LIMIT %s
            FOR UPDATE OF m SKIP LOCKED
            """,
            (cfg.embed_worker_max_chunk_retries, batch),
        )
        rows = cur.fetchall()
        if not rows:
            return 0
        for mid, subj, fa, fn, to, ds, body in rows:
            cur.execute("SAVEPOINT msg")
            try:
                msg = MessageRow(
                    id=mid, subject=subj, from_addr=fa, from_name=fn,
                    to_addrs=to, date_sent=ds, body_text=body,
                )
                for spec in chunk_message(msg, cfg):
                    cur.execute(
                        "INSERT INTO message_chunks (message_id, kind, chunk_idx,"
                        " text, token_count) VALUES (%s, %s, %s, %s, %s)"
                        " ON CONFLICT (message_id, kind, chunk_idx) DO NOTHING",
                        (mid, spec.kind, spec.chunk_idx, spec.text, spec.token_count),
                    )
                cur.execute("RELEASE SAVEPOINT msg")
            except Exception as exc:  # noqa: BLE001 — poison-pill isolation
                cur.execute("ROLLBACK TO SAVEPOINT msg")
                log.warning("chunking failed for message %s: %s", mid, exc)
                record_failed_chunking(conn, mid, exc)
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
    """Embed claimed chunks; UPDATE per chunk inside a SAVEPOINT for poison isolation.

    The backend.embed_documents() call is NOT inside a per-chunk SAVEPOINT —
    if it raises, the whole batch is rolled back by the caller (batch-level
    fallback), which is the correct behavior for transient backend errors.
    """
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
                log.warning("embedding write failed for chunk %s: %s", cid, exc)
                record_failed_embedding(conn, "message_chunks", cid, exc)
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

    Batch-level backend errors are logged and the transaction is rolled
    back so the FOR UPDATE locks release — chunks get re-claimed next
    sweep. Operators see the WARNING and intervene; the worker doesn't
    silently poison every queued chunk.
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
        log.warning("embed_worker batch failed (will retry next sweep): %s",
                    exc, exc_info=True)
        conn.rollback()
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
