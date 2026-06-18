# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Attachment extraction worker.

Polls ``attachment_blobs`` for MIME-allowlisted blobs without an
``attachment_text`` row, runs ``LightweightExtractor`` first and
``DoclingExtractor`` as a PDF-only fallback, and writes
``attachment_text`` rows (or sentinel / ``failed_extractions`` rows per
the per-blob decision tree described below).

Per-blob decision tree
----------------------
1. ``size_bytes > cfg.extractor_max_blob_bytes``
   → INSERT sentinel ``extractor='size-skipped'``, ``extracted_text=''``.
2. Try ``LightweightExtractor.extract()``.
3. text non-empty → INSERT ``attachment_text`` row with lightweight extractor
   name + page_count.  Done.
4. lightweight returned empty OR raised:
   a. PDF AND docling importable → try DoclingExtractor.
      - docling text non-empty → INSERT ``attachment_text``.  Done.
      - docling raised → record failure (extractor='docling').  Done.
      - docling empty + lw raised → record failure (extractor='lightweight').
      - docling empty + lw empty → INSERT sentinel ``extractor='lightweight-empty'``.
   b. Non-PDF OR docling missing:
      - If PDF and docling missing → emit one-shot WARNING via
        ``warn_docling_missing()``.
      - lw raised → record failure (extractor='lightweight').
      - lw empty → INSERT sentinel ``extractor='lightweight-empty'``.

SAVEPOINT discipline (mirrors embed_worker.py)
----------------------------------------------
- Per-blob ``SAVEPOINT extract_blob`` isolates each blob so a poison blob
  only loses itself, not the batch.
- ``_record_failure`` is called inside a nested ``SAVEPOINT extract_fail_log``
  so a logging failure can't abort the outer transaction.
- ``conn.commit()`` is called once at the end of the batch (not per-blob).

Transient vs poison-pill classification (#36, #47)
--------------------------------------------------
- ``TransientExtractorError`` and built-in ``ConnectionError`` /
  ``TimeoutError`` / ``MemoryError`` (anywhere in the cause chain) are
  treated as *transient*: ROLLBACK to ``extract_blob``, WARNING log, no
  ``failed_extractions`` row. The blob stays eligible for the next sweep
  with retry_count untouched — so a docling model-download blip or a
  one-off OOM doesn't permanently poison a perfectly fine PDF.
- ``_TRANSIENT_EXC_TYPES`` stays deliberately narrow (builtin classes
  only) — a broader allowlist like plain ``OSError`` would mis-classify
  permanent ``ENOENT``/``EACCES`` as transient. docling's *third-party*
  network classes (``requests`` / ``httpx`` / ``urllib3`` /
  ``huggingface_hub``) are NOT in the builtin hierarchy, so they can't be
  recognised here. Instead ``DoclingExtractor.extract`` opts them in at the
  wrapper (#47): a ``convert()`` failure whose chain contains a
  ``extractor._TRANSIENT_THIRD_PARTY_MODULES`` package is re-raised as
  ``TransientExtractorError``, which ``_is_transient`` then recognises.
- Everything else (corrupt PDF, encrypted file, parser raise, unexpected
  RuntimeError from a poison blob) is treated as a *poison pill*: ROLLBACK,
  upsert ``failed_extractions`` with retry_count += 1, permanently skipped
  once retry_count >= ``cfg.extract_worker_max_retries``. Mirrors the
  embed_worker's batch-level rollback policy for backend errors.
- Precedence when both extractors raise: docling's exception wins (it's
  raised last, and lightweight's is held in ``lw_raised``). If docling is
  transient the whole blob is treated as transient and the underlying
  lightweight failure is not recorded — on the next sweep docling will
  usually succeed and supersede lightweight anyway, so the masking is
  self-correcting in practice.

Allowlist filter is applied in Python (not SQL) because the allowlist lists
live in ``SearchConfig`` and may be customised per-deployment.

The master enable flag ``cfg.run_extract_worker`` short-circuits the whole
worker to return 0 when False, consistent with ``run_embed_worker_once``.
"""

from __future__ import annotations

import logging
import threading
import traceback as tb_mod
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from localmail.config import SearchConfig
from localmail.heartbeat import safe_heartbeat
from localmail.search.extractor import (
    DoclingExtractor,
    ExtractedText,
    ExtractorError,
    LightweightExtractor,
    TransientExtractorError,
    _try_import_docling,
    iter_exc_chain,
    warn_docling_missing,
)

_LOG = logging.getLogger(__name__)


_TRANSIENT_EXC_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    MemoryError,
)
"""Built-in exception classes treated as transient when found anywhere in
an extractor exception's cause chain. Network blips, model-download
timeouts, and OOM rarely indicate a poison-pill blob — they're worth
retrying. Narrow on purpose: a broader allowlist (e.g. plain ``OSError``)
would mis-classify permanent ENOENT/EACCES failures as transient and let
genuinely broken blobs loop forever."""


def _is_transient(exc: BaseException) -> bool:
    """True iff ``exc`` (or any cause/context in its chain) signals a
    transient extraction failure.

    Recognises ``TransientExtractorError`` (extractors opt in directly,
    including docling's third-party network classes — see
    ``extractor._TRANSIENT_THIRD_PARTY_MODULES``) and the narrow builtin
    ``_TRANSIENT_EXC_TYPES``. The chain walk (``__cause__`` then
    ``__context__`` unless suppressed) is shared with the extractor via
    ``iter_exc_chain``.
    """
    return any(
        isinstance(e, (TransientExtractorError, *_TRANSIENT_EXC_TYPES))
        for e in iter_exc_chain(exc)
    )


def transient_budget_exhausted(transient_count: int, max_transient_retries: int) -> bool:
    """True iff a blob's accumulated *consecutive* transient extraction
    failures have reached the configured cap, so it should no longer be
    re-attempted (#153).

    Inclusive on the cap: ``count >= max`` — matches the SQL claim filter
    ``transient_count < max`` (a blob at the cap is excluded). Independent of
    ``failed_extractions.retry_count``, which stays reserved for poison-pills.
    """
    return transient_count >= max_transient_retries


def _no_nul(s: str) -> str:
    """Strip NUL bytes so the string is safe for a Postgres TEXT column.

    A third-party/docling exception message can carry a NUL byte (mangled
    remote payload); Postgres TEXT rejects it, which would abort the failure
    INSERT. On the transient path that abort means the counter never
    increments — the blob would loop forever, defeating the #153 cap.
    """
    return s.replace("\x00", "") if "\x00" in s else s


def _is_allowlisted(mime_type: str | None, path: str, cfg: SearchConfig) -> bool:
    """Return True iff the blob's MIME type or filename extension is allowlisted.

    Checks ``cfg.extractor_mime_allowlist`` first, then
    ``cfg.extractor_extension_allowlist``.  Both comparisons are
    case-insensitive.  Either match is sufficient.
    """
    mt = (mime_type or "").lower()
    if mt in (m.lower() for m in cfg.extractor_mime_allowlist):
        return True
    ext = Path(path).suffix.lower()
    return ext in (e.lower() for e in cfg.extractor_extension_allowlist)


def _is_pdf(mime_type: str | None, path: str) -> bool:
    """Return True iff the blob is a PDF by MIME type or filename extension."""
    mt = (mime_type or "").lower()
    return mt == "application/pdf" or Path(path).suffix.lower() == ".pdf"


def _claim_batch(conn: psycopg.Connection, cfg: SearchConfig) -> list[tuple]:
    """Select up to ``cfg.extract_worker_batch_size`` eligible blobs.

    A blob is eligible when:
    - No ``attachment_text`` row exists yet (not yet processed).
    - Either no ``failed_extractions`` row, OR
      ``failed_extractions.retry_count < cfg.extract_worker_max_retries``.
    - Either no ``transient_extractions`` row, OR
      ``transient_extractions.transient_count <
      cfg.extract_worker_max_transient_retries`` (#153) — a blob that has
      exhausted its *consecutive* transient budget stops being re-attempted.

    Returns ``(sha256, path, mime_type, size_bytes, transient_count)`` tuples;
    ``transient_count`` is ``None`` when the blob has no transient history (used
    by the caller to skip the reset DELETE on the common no-history path).

    Rows are ordered by ``attachment_blobs.first_seen_at`` so oldest blobs
    are processed first (FIFO, consistent with email archive sync order).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.sha256, b.path, b.mime_type, b.size_bytes, tr.transient_count
            FROM attachment_blobs b
            LEFT JOIN attachment_text     t  USING (sha256)
            LEFT JOIN failed_extractions  f  USING (sha256)
            LEFT JOIN transient_extractions tr USING (sha256)
            WHERE t.sha256 IS NULL
              AND (f.sha256 IS NULL OR f.retry_count < %s)
              AND (tr.sha256 IS NULL OR tr.transient_count < %s)
            ORDER BY b.first_seen_at
            LIMIT %s
            FOR UPDATE OF b SKIP LOCKED
            """,
            (
                cfg.extract_worker_max_retries,
                cfg.extract_worker_max_transient_retries,
                cfg.extract_worker_batch_size,
            ),
        )
        return list(cur.fetchall())


def _record_failure(
    conn: psycopg.Connection,
    sha256: bytes,
    extractor_name: str,
    exc: BaseException,
) -> None:
    """Upsert a ``failed_extractions`` row for the given blob.

    On conflict (same sha256 already failed previously) the row is updated
    with the latest error details and ``retry_count`` is incremented by 1.

    Must be called inside a nested SAVEPOINT (see ``_record_failure_safely``).

    Args:
        conn: Active psycopg connection with an open transaction.
        sha256: Raw SHA-256 digest bytes identifying the blob.
        extractor_name: Name of the extractor that failed (e.g. 'lightweight',
            'docling').
        exc: The exception that caused the failure.
    """
    _LOG.warning(
        "recording extraction failure for blob %s via extractor %r: %s",
        sha256.hex(),
        extractor_name,
        exc,
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO failed_extractions
                (sha256, extractor, error_class, error_message, traceback,
                 retry_count, last_retry_at)
            VALUES (%s, %s, %s, %s, %s, 1, now())
            ON CONFLICT (sha256) DO UPDATE
                SET extractor      = EXCLUDED.extractor,
                    error_class    = EXCLUDED.error_class,
                    error_message  = EXCLUDED.error_message,
                    traceback      = EXCLUDED.traceback,
                    retry_count    = failed_extractions.retry_count + 1,
                    last_retry_at  = now()
            """,
            (
                sha256,
                extractor_name,
                type(exc).__name__,
                _no_nul(str(exc)),
                _no_nul(
                    "".join(
                        tb_mod.format_exception(type(exc), exc, exc.__traceback__)
                    )
                ),
            ),
        )


def _record_failure_safely(
    conn: psycopg.Connection,
    sha256: bytes,
    extractor_name: str,
    exc: BaseException,
) -> bool:
    """Wrap ``_record_failure`` in a nested SAVEPOINT.

    Returns True on success, False if even the failure-recording itself
    failed (in which case a single ``_LOG.exception`` line is the only
    evidence of the failure).
    """
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT extract_fail_log")
    try:
        _record_failure(conn, sha256, extractor_name, exc)
        with conn.cursor() as cur:
            cur.execute("RELEASE SAVEPOINT extract_fail_log")
        return True
    except Exception:  # noqa: BLE001
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT extract_fail_log")
        _LOG.exception(
            "failed to record extraction failure for blob %s", sha256.hex()
        )
        return False


def _record_transient(
    conn: psycopg.Connection,
    sha256: bytes,
    exc: BaseException,
) -> int:
    """Upsert a ``transient_extractions`` row, bumping ``transient_count``.

    Returns the new (post-increment) ``transient_count``. On first failure the
    row is inserted with count 1; on conflict the count is incremented. Counts
    *consecutive* transient failures — ``_clear_transient`` resets it on a
    successful extraction. Independent of ``failed_extractions.retry_count``.

    Must be called inside a nested SAVEPOINT (see ``_record_transient_safely``).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transient_extractions
                (sha256, transient_count, error_class, error_message,
                 first_transient_at, last_transient_at)
            VALUES (%s, 1, %s, %s, now(), now())
            ON CONFLICT (sha256) DO UPDATE
                SET transient_count   = transient_extractions.transient_count + 1,
                    error_class       = EXCLUDED.error_class,
                    error_message     = EXCLUDED.error_message,
                    last_transient_at = now()
            RETURNING transient_count
            """,
            (sha256, type(exc).__name__, _no_nul(str(exc))),
        )
        row = cur.fetchone()
    assert row is not None  # RETURNING on an upsert always yields a row
    return int(row[0])


def _record_transient_safely(
    conn: psycopg.Connection,
    sha256: bytes,
    exc: BaseException,
) -> int | None:
    """Wrap ``_record_transient`` in a nested SAVEPOINT.

    Returns the new ``transient_count`` on success, or ``None`` if the
    counter write itself failed (a single ``_LOG.exception`` line is then the
    only evidence). The outer transaction is never aborted by a logging
    failure — mirrors ``_record_failure_safely``.
    """
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT extract_transient_log")
    try:
        count = _record_transient(conn, sha256, exc)
        with conn.cursor() as cur:
            cur.execute("RELEASE SAVEPOINT extract_transient_log")
        return count
    except Exception:  # noqa: BLE001
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT extract_transient_log")
        _LOG.exception(
            "failed to record transient extraction failure for blob %s",
            sha256.hex(),
        )
        return None


def _clear_transient(conn: psycopg.Connection, sha256: bytes) -> None:
    """Delete any ``transient_extractions`` row for the blob.

    Called after a successful extraction so the cap counts only *consecutive*
    transient failures. A no-op when no row exists; the caller gates this on
    the blob having transient history so the common path pays nothing.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM transient_extractions WHERE sha256 = %s",
            (sha256,),
        )


def _insert_attachment_text(
    conn: psycopg.Connection,
    sha256: bytes,
    et: ExtractedText,
) -> None:
    """INSERT an ``attachment_text`` row for the given blob.

    Uses ``ON CONFLICT DO NOTHING`` so that a re-run after a partial commit
    does not fail — idempotent like all other sync-path inserts.

    Args:
        conn: Active psycopg connection with an open transaction.
        sha256: Raw SHA-256 digest bytes identifying the blob.
        et: The extraction result to persist.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text "
            "    (sha256, extractor, extracted_text, page_count) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (sha256) DO NOTHING",
            (sha256, et.extractor, et.text, et.page_count),
        )


def _process_blob(
    conn: psycopg.Connection,
    sha256: bytes,
    path: str,
    mime_type: str | None,
    size_bytes: int,
    cfg: SearchConfig,
    lw: LightweightExtractor,
    dl: DoclingExtractor,
) -> bool:
    """Apply the per-blob decision tree and write the result to the DB.

    Returns True when an ``attachment_text`` row was written (including
    sentinels such as 'size-skipped' and 'lightweight-empty').  Returns
    False when the blob lands in ``failed_extractions`` instead.

    This function is always called inside a per-blob SAVEPOINT by
    ``run_extract_worker_once``; it must NOT commit or issue its own
    SAVEPOINT/ROLLBACK.  Failure recording via ``_record_failure`` is also
    the caller's responsibility at the outer level when this function raises
    unexpectedly — but _process_blob itself calls ``_record_failure`` for the
    planned failure paths (lightweight/docling raised) and does NOT re-raise
    in those cases.

    Args:
        conn: Active psycopg connection with an open transaction.
        sha256: Raw SHA-256 digest bytes identifying the blob.
        path: On-disk path string for the blob file.
        mime_type: MIME type as stored in ``attachment_blobs``.
        size_bytes: Blob size in bytes.
        cfg: SearchConfig providing all tunables.
        lw: Shared ``LightweightExtractor`` instance for this batch.
        dl: Shared ``DoclingExtractor`` instance for this batch.
    """
    # Step 1: size guard.
    if size_bytes > cfg.extractor_max_blob_bytes:
        _insert_attachment_text(
            conn,
            sha256,
            ExtractedText(text="", page_count=None, extractor="size-skipped"),
        )
        return True

    blob_path = Path(path)

    # Step 2: try lightweight extractor.
    lw_text: ExtractedText | None = None
    lw_raised: BaseException | None = None
    try:
        lw_text = lw.extract(blob_path, mime_type)
    except Exception as exc:
        lw_raised = exc

    # Step 3: lightweight produced non-empty text — done.
    if lw_text is not None and lw_text.text:
        _insert_attachment_text(conn, sha256, lw_text)
        return True

    # Step 4: lightweight returned empty or raised.
    is_pdf = _is_pdf(mime_type, path)
    docling_avail = _try_import_docling() is not None

    if is_pdf and docling_avail:
        # Step 4a: try docling fallback for PDFs.
        try:
            dl_text = dl.extract(blob_path, mime_type)
        except Exception as exc:
            # Transient (network blip during model fetch, OOM): propagate
            # so the outer SAVEPOINT handler rolls back without recording —
            # the blob stays eligible for the next sweep.
            if _is_transient(exc):
                raise
            _record_failure_safely(conn, sha256, dl.name, exc)
            return False

        if dl_text.text:
            # Docling produced text — done.
            _insert_attachment_text(conn, sha256, dl_text)
            return True

        # Docling returned empty.
        if lw_raised is not None:
            if _is_transient(lw_raised):
                raise lw_raised
            _record_failure_safely(conn, sha256, lw.name, lw_raised)
            return False

        # Both extractors returned empty — insert lightweight-empty sentinel.
        _insert_attachment_text(
            conn,
            sha256,
            ExtractedText(
                text="", page_count=None, extractor="lightweight-empty"
            ),
        )
        return True

    # Step 4b: non-PDF or docling not installed.
    if is_pdf and not docling_avail:
        warn_docling_missing()

    if lw_raised is not None:
        if _is_transient(lw_raised):
            raise lw_raised
        _record_failure_safely(conn, sha256, lw.name, lw_raised)
        return False

    # Lightweight returned empty — insert sentinel.
    _insert_attachment_text(
        conn,
        sha256,
        ExtractedText(text="", page_count=None, extractor="lightweight-empty"),
    )
    return True


def run_extract_worker_once(conn: psycopg.Connection, cfg: SearchConfig) -> int:
    """Run one batch of the extraction worker; return count of blobs touched.

    "Touched" means an ``attachment_text`` row was written (including size-skipped
    and lightweight-empty sentinels) OR a ``failed_extractions`` row was
    written/upserted.  Blobs that are skipped because they are not in the
    allowlist are NOT counted.

    The master enable flag ``cfg.run_extract_worker`` short-circuits to 0
    immediately when False, consistent with ``run_embed_worker_once``.

    Per-blob SAVEPOINT discipline:
    - Each blob's DB work runs inside ``SAVEPOINT extract_blob``.
    - On unexpected exception (not the planned failure paths handled inside
      ``_process_blob``): ROLLBACK to the savepoint, then record the failure
      inside a nested ``SAVEPOINT extract_fail_log`` so a logging failure
      can't kill the outer transaction.
    - ``conn.commit()`` is called once after the entire batch.

    Args:
        conn: Active psycopg connection (autocommit=False).
        cfg: SearchConfig providing all tunables.

    Returns:
        Number of blobs for which a result row was written (``attachment_text``
        or ``failed_extractions``).
    """
    if not cfg.run_extract_worker:
        return 0

    batch = _claim_batch(conn, cfg)
    if not batch:
        conn.commit()
        return 0

    lw = LightweightExtractor(cfg)
    dl = DoclingExtractor(cfg)
    touched = 0

    for sha256, path, mime_type, size_bytes, transient_count in batch:
        if not _is_allowlisted(mime_type, path, cfg):
            # Silently skip; not counted in touched.
            continue

        with conn.cursor() as cur:
            cur.execute("SAVEPOINT extract_blob")

        try:
            wrote = _process_blob(
                conn, sha256, path, mime_type, size_bytes, cfg, lw, dl
            )
            if wrote and transient_count is not None:
                # A prior transient streak recovered — reset the counter so
                # the cap measures *consecutive* failures (#153).
                _clear_transient(conn, sha256)
            with conn.cursor() as cur:
                cur.execute("RELEASE SAVEPOINT extract_blob")
            # Count both successful writes and planned failures recorded
            # inside _process_blob (e.g. docling raised).
            touched += 1
        except Exception as exc:  # noqa: BLE001 — outer safety net
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT extract_blob")
            if _is_transient(exc):
                # Transient: no failed_extractions row (retry_count untouched),
                # but bump the independent transient counter so a *permanently*
                # failing third-party network error (#153) eventually stops
                # being re-attempted instead of looping every sweep forever.
                new_count = _record_transient_safely(conn, sha256, exc)
                if new_count is not None and transient_budget_exhausted(
                    new_count, cfg.extract_worker_max_transient_retries
                ):
                    _LOG.warning(
                        "transient extractor budget exhausted for blob %s "
                        "after %d consecutive failures — giving up "
                        "(clear via retry-failed-extractions): %s",
                        sha256.hex(),
                        new_count,
                        exc,
                    )
                else:
                    _LOG.warning(
                        "transient extractor error for blob %s "
                        "(will retry next sweep): %s",
                        sha256.hex(),
                        exc,
                    )
            elif _record_failure_safely(conn, sha256, "unexpected", exc):
                touched += 1

    conn.commit()
    return touched


_INITIAL_BACKOFF_S = 1.0
"""Starting backoff when a connection acquisition or sweep raises."""

_MAX_BACKOFF_S = 60.0
"""Cap on the doubling backoff; matches the daemon's IDLE/poll reconnect cap."""


def run_extract_worker(
    *,
    pool: ConnectionPool,
    cfg: SearchConfig,
    stop_event: threading.Event,
) -> None:
    """Background loop: drain the extraction queue, sleep, repeat.

    Acquires a fresh connection from ``pool`` for each drain iteration via
    ``pool.connection()`` so server-side idle timeouts and transaction-state
    leaks cannot accumulate across sweeps. The pool slot is released before
    the inter-sweep sleep so the connection isn't pinned during the (long)
    poll interval. Reconnects with exponential backoff (1 s → 60 s cap) when
    the pool raises. Exits cleanly as soon as ``stop_event`` is set —
    including during the inter-sweep sleep so the thread joins promptly.

    Args:
        pool: The shared daemon ``ConnectionPool``. The worker borrows a
            connection per drain via ``pool.connection()`` and releases it
            before each idle sleep.
        cfg: ``SearchConfig`` supplying all tunables: poll interval, batch
            size, retry cap, and the master enable flag.
        stop_event: Set this to request graceful shutdown. The loop checks
            the event before each sweep and uses ``stop_event.wait`` for
            inter-sweep sleeps so cancellation is near-instantaneous.
    """
    backoff = _INITIAL_BACKOFF_S
    while not stop_event.is_set():
        safe_heartbeat(pool, worker_kind="extract", account_id=None, state="idle")
        try:
            with pool.connection() as conn:
                while not stop_event.is_set():
                    touched = run_extract_worker_once(conn, cfg)
                    if touched == 0:
                        break
        except Exception as exc:
            _LOG.exception("extract_worker: error during sweep")
            safe_heartbeat(pool, worker_kind="extract", account_id=None,
                           state="error", last_error_msg=str(exc))
            if stop_event.wait(timeout=backoff):
                return
            backoff = min(backoff * 2, _MAX_BACKOFF_S)
            continue

        backoff = _INITIAL_BACKOFF_S
        if stop_event.is_set():
            break
        if stop_event.wait(timeout=cfg.extract_worker_poll_interval_s):
            break
