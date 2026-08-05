# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Background worker: fill embeddings for message_chunks and attachment_chunks.

Phase 1 handled message_chunks only; Phase 2 extends the worker to also chunk
attachment_text rows into attachment_chunks and embed them via the same sweep.
The worker is account-agnostic — one instance per process, since embedding
throughput is backend-bound rather than IMAP-bound.

Failure model (mirrors sync.py poison-pill handling):
  - Per-message SAVEPOINT around chunk_message() + chunk INSERTs. A poison
    message lands in failed_chunkings (keyed by message_id) and is skipped
    on subsequent sweeps once retry_count >= embed_worker_max_chunk_retries.
  - Per-blob SAVEPOINT around chunk_attachment_text() + chunk INSERTs. A
    poison blob is logged and skipped (no dedicated failure table for blobs;
    the next sweep will retry it until the blob's chunks finally appear).
  - Per-chunk SAVEPOINT around the embedding UPDATE. A poison chunk lands
    in failed_embeddings and is skipped likewise. The chunk_table column in
    failed_embeddings identifies whether the source was message_chunks or
    attachment_chunks.
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
from localmail.heartbeat import safe_heartbeat
from localmail.search.chunking import MessageRow, chunk_attachment_text, chunk_message
from localmail.search.embeddings import EmbeddingBackend
from localmail.search.lang_detect import LanguageDetector, run_lang_detect_pass
from localmail.search.sweep_pacing import (
    SweepOutcome,
    next_idle_streak,
    sweep_sleep_seconds,
)

_CHUNK_TABLES = frozenset({"message_chunks", "attachment_chunks"})

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


def _chunk_attachments_lazily(conn: psycopg.Connection, cfg: SearchConfig, batch: int) -> int:
    """Chunk attachment_text rows that don't yet have attachment_chunks rows.

    Skips sentinel rows where extracted_text='' (produced when an attachment is
    skipped due to size limits or unsupported format). Per-blob SAVEPOINT mirrors
    _chunk_messages_lazily — a single broken blob is logged and skipped rather
    than aborting the whole batch. Returns the number of blobs processed.

    Unlike message chunking, blob-level failures are not recorded in a dedicated
    failure table; the next sweep will retry the blob until its chunks appear.
    Persistent failures surface via repeated WARNING log lines.

    A claimed row that chunks to nothing is healed to the '' sentinel in place
    (#266): its text passed the `<> ''` filter yet normalises to empty, so
    without the heal it would be re-claimed on every sweep forever — and
    enough such rows sorting low in the sha256 order fill the batch and stop
    attachment ingestion archive-wide, the #216 shape. `ExtractedText` now
    normalises whitespace-only text at the boundary, so this is the backstop
    for legacy rows (and for any future drift in what the chunker calls
    empty — the trigger is the chunker's own [] verdict, which cannot drift).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.sha256, t.extracted_text
            FROM attachment_text t
            LEFT JOIN attachment_chunks c USING (sha256)
            WHERE t.extracted_text <> ''
              AND c.sha256 IS NULL
            ORDER BY t.sha256
            LIMIT %s
            FOR UPDATE OF t SKIP LOCKED
            """,
            (batch,),
        )
        rows = cur.fetchall()

    if not rows:
        conn.commit()
        return 0

    for sha256_bytes, text in rows:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT chunk_blob")
            try:
                specs = chunk_attachment_text(sha256_bytes, text, cfg)
                if not specs:
                    cur.execute(
                        "UPDATE attachment_text SET extracted_text = ''"
                        " WHERE sha256 = %s",
                        (sha256_bytes,),
                    )
                    log.info(
                        "attachment_text for %s chunked to nothing;"
                        " healed to the '' sentinel (#266)",
                        sha256_bytes.hex()
                        if isinstance(sha256_bytes, (bytes, bytearray))
                        else sha256_bytes,
                    )
                for spec in specs:
                    cur.execute(
                        "INSERT INTO attachment_chunks"
                        " (sha256, chunk_idx, text, token_count)"
                        " VALUES (%s, %s, %s, %s)"
                        " ON CONFLICT (sha256, chunk_idx) DO NOTHING",
                        (sha256_bytes, spec.chunk_idx, spec.text, spec.token_count),
                    )
                cur.execute("RELEASE SAVEPOINT chunk_blob")
            except Exception as exc:  # noqa: BLE001 — poison-pill isolation
                cur.execute("ROLLBACK TO SAVEPOINT chunk_blob")
                log.warning(
                    "chunking failed for attachment blob %s: %s",
                    sha256_bytes.hex() if isinstance(sha256_bytes, (bytes, bytearray)) else sha256_bytes,
                    exc,
                )
    conn.commit()
    return len(rows)


def _claim_unembedded(
    cur,
    cfg: SearchConfig,
    chunk_table: str,
) -> list[tuple[int, str]]:
    """Select up to cfg.embed_worker_batch_size unembedded rows from chunk_table.

    Uses FOR UPDATE SKIP LOCKED so concurrent workers don't claim the same rows.
    Rows whose retry_count in failed_embeddings has reached the configured maximum
    are excluded — they are permanently poisoned and skipped.

    Args:
        cur: An open psycopg cursor (must be inside an active transaction).
        cfg: Search configuration; supplies batch size and max-retry threshold.
        chunk_table: Must be one of 'message_chunks' or 'attachment_chunks'.
            Validated against a whitelist before use in SQL.
    """
    assert chunk_table in _CHUNK_TABLES, f"unknown chunk_table: {chunk_table!r}"
    # chunk_table is validated against a closed whitelist above; safe to
    # interpolate as an identifier rather than a %s placeholder, because
    # psycopg does not support table-name placeholders in SELECT.
    cur.execute(
        f"""
        SELECT c.id, c.text FROM {chunk_table} c
        WHERE c.embedding_v1 IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM failed_embeddings fe
              WHERE fe.chunk_table = %s AND fe.chunk_id = c.id
                AND fe.retry_count >= %s
          )
        ORDER BY c.id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (chunk_table, cfg.embed_worker_max_chunk_retries, cfg.embed_worker_batch_size),
    )
    return cur.fetchall()


def _embed_and_store(
    conn,
    cfg: SearchConfig,
    backend: EmbeddingBackend,
    claimed: list[tuple[int, str]],
    chunk_table: str,
) -> int:
    """Embed claimed chunks; UPDATE each inside a SAVEPOINT for poison isolation.

    The backend.embed_documents() call covers the whole batch — if it raises,
    the caller rolls back (batch-level fallback), which is the correct behavior
    for transient backend errors. Individual UPDATE failures are caught per-chunk
    and recorded in failed_embeddings so the chunk is skipped on subsequent sweeps.

    Args:
        conn: Active psycopg connection.
        cfg: Search configuration (unused here but kept for symmetry with callers).
        backend: The embedding backend to call.
        claimed: List of (chunk_id, text) pairs returned by _claim_unembedded.
        chunk_table: Must be one of 'message_chunks' or 'attachment_chunks'.
            Validated against a whitelist before use in SQL.

    Returns:
        Number of chunks successfully written.
    """
    assert chunk_table in _CHUNK_TABLES, f"unknown chunk_table: {chunk_table!r}"
    texts = [t for _, t in claimed]
    vectors = backend.embed_documents(texts)
    written = 0
    for (cid, _text), vec in zip(claimed, vectors, strict=True):
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT chunk")
            try:
                # chunk_table validated against whitelist above; f-string is safe.
                cur.execute(
                    f"UPDATE {chunk_table} SET embedding_v1 = %s::halfvec,"  # noqa: S608
                    " embedded_at = now() WHERE id = %s",
                    (vec, cid),
                )
                cur.execute("RELEASE SAVEPOINT chunk")
                written += 1
            except Exception as exc:  # noqa: BLE001 — poison-pill isolation
                cur.execute("ROLLBACK TO SAVEPOINT chunk")
                log.warning("embedding write failed for chunk %s in %s: %s", cid, chunk_table, exc)
                record_failed_embedding(conn, chunk_table, cid, exc)
    conn.commit()
    return written


def _embed_table(
    conn: psycopg.Connection,
    cfg: SearchConfig,
    backend: EmbeddingBackend,
    chunk_table: str,
) -> int:
    """Claim unembedded rows from chunk_table and embed them in one batch.

    Returns the number of chunks newly embedded. A batch-level backend error
    rolls back and returns 0 so that the FOR UPDATE locks are released and
    chunks get re-claimed on the next sweep.

    Args:
        conn: Active psycopg connection.
        cfg: Search configuration; supplies batch size and retry thresholds.
        backend: The embedding backend to call.
        chunk_table: Must be 'message_chunks' or 'attachment_chunks'.
    """
    with conn.cursor() as cur:
        claimed = _claim_unembedded(cur, cfg, chunk_table)
    if not claimed:
        conn.commit()
        return 0
    try:
        return _embed_and_store(conn, cfg, backend, claimed, chunk_table)
    except Exception as exc:  # noqa: BLE001 — batch-level fallback
        log.warning(
            "embed_worker batch failed for %s (will retry next sweep): %s",
            chunk_table,
            exc,
            exc_info=True,
        )
        conn.rollback()
        return 0


def run_embed_worker_once(
    conn: psycopg.Connection,
    cfg: SearchConfig,
    backend: EmbeddingBackend,
    *,
    lang_detector: LanguageDetector | None = None,
) -> SweepOutcome:
    """One sweep: chunk pending messages + attachments, then embed pending chunks.

    Processing order:
      1. _chunk_messages_lazily — fills message_chunks for unchunked messages.
      2. _chunk_attachments_lazily — fills attachment_chunks for unchunked
         attachment_text rows (Phase 2). Sentinel rows (extracted_text='')
         produce zero chunks and are silently skipped.
      3. run_lang_detect_pass — populates messages.body_lang for messages
         that don't yet have it (only when `lang_detector` is provided).
      4. Embed pending message_chunks rows.
      5. Embed pending attachment_chunks rows.

    Returns a `SweepOutcome` carrying the chunks embedded across both tables
    *and* the language rows visited. Used both by the background daemon thread
    and the `localmail embed-backfill` CLI.

    The language count is part of the result rather than a silent side effect
    because `run_embed_worker`'s backoff reads it: this function used to return
    a bare embedded-chunk count, so a sweep that laboured through a full
    `body_lang_detect_batch_size` slice reported 0, the loop concluded the
    queue was empty, and it slept the full backoff (#259). Which queue advanced
    is the caller's business; *whether* one did is not.

    Batch-level backend errors are logged and the transaction is rolled back so
    the FOR UPDATE locks release — chunks get re-claimed next sweep. Operators
    see the WARNING and intervene; the worker doesn't silently poison every
    queued chunk.
    """
    chunk_batch = max(cfg.embed_worker_batch_size, cfg.embed_worker_chunk_batch_size)
    _chunk_messages_lazily(conn, cfg, batch=chunk_batch)
    _chunk_attachments_lazily(conn, cfg, batch=chunk_batch)
    lang_visited = 0
    if lang_detector is not None and cfg.body_lang_enabled:
        lang_visited = run_lang_detect_pass(conn, cfg, lang_detector).visited
    embedded_msg = _embed_table(conn, cfg, backend, "message_chunks")
    embedded_att = _embed_table(conn, cfg, backend, "attachment_chunks")
    return SweepOutcome(
        embedded=embedded_msg + embedded_att, lang_visited=lang_visited,
    )


def run_embed_worker(
    stop: threading.Event,
    pool,
    cfg: SearchConfig,
    backend: EmbeddingBackend,
    *,
    lang_detector: LanguageDetector | None = None,
) -> None:
    """Background loop: sleep, sweep, sleep. Exits when `stop` is set.

    Re-acquires a fresh connection from the pool each sweep to keep the
    pool's idle-rotation healthy. Backs off on consecutive empty sweeps so an
    empty queue doesn't busy-poll — where "empty" means neither the embedding
    queue nor the language-detection queue advanced (#259). Both the progress
    predicate and the arithmetic live in `sweep_pacing`.

    A sweep that *raises* is paced as an empty one — that covers pool
    acquisition and anything outside `_embed_table`'s own handler. It does not
    cover a broken embedding backend: `_embed_table` catches batch-level
    backend errors itself, logs, and returns 0, so such a sweep is only "empty"
    when nothing else advanced. While a language backlog drains, a persistently
    broken backend is therefore retried once per base poll interval rather than
    once per backoff ceiling, and its WARNING repeats at that rate.
    """
    streak = 0
    while not stop.is_set():
        safe_heartbeat(pool, worker_kind="embed", account_id=None, state="idle")
        try:
            with pool.connection() as conn:
                outcome = run_embed_worker_once(
                    conn, cfg, backend, lang_detector=lang_detector,
                )
        except Exception as exc:  # noqa: BLE001
            log.error("embed_worker sweep error: %s", exc, exc_info=True)
            safe_heartbeat(pool, worker_kind="embed", account_id=None,
                           state="error", last_error_msg=str(exc))
            outcome = SweepOutcome(embedded=0, lang_visited=0)
        streak = next_idle_streak(
            streak,
            made_progress=outcome.made_progress,
            max_steps=cfg.embed_worker_idle_backoff_max_steps,
        )
        stop.wait(timeout=sweep_sleep_seconds(streak, cfg.embed_worker_poll_interval_s))
