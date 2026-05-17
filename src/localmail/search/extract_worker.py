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
from typing import Callable

import psycopg

from localmail.config import SearchConfig
from localmail.search.extractor import (
    DoclingExtractor,
    ExtractedText,
    ExtractorError,
    LightweightExtractor,
    _try_import_docling,
    warn_docling_missing,
)

_LOG = logging.getLogger(__name__)


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

    Rows are ordered by ``attachment_blobs.first_seen_at`` so oldest blobs
    are processed first (FIFO, consistent with email archive sync order).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.sha256, b.path, b.mime_type, b.size_bytes
            FROM attachment_blobs b
            LEFT JOIN attachment_text  t USING (sha256)
            LEFT JOIN failed_extractions f USING (sha256)
            WHERE t.sha256 IS NULL
              AND (f.sha256 IS NULL OR f.retry_count < %s)
            ORDER BY b.first_seen_at
            LIMIT %s
            """,
            (cfg.extract_worker_max_retries, cfg.extract_worker_batch_size),
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
            VALUES (%s, %s, %s, %s, %s, 0, now())
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
                str(exc),
                "".join(
                    tb_mod.format_exception(type(exc), exc, exc.__traceback__)
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
            # Docling raised → record docling failure.
            _record_failure_safely(conn, sha256, dl.name, exc)
            return False

        if dl_text.text:
            # Docling produced text — done.
            _insert_attachment_text(conn, sha256, dl_text)
            return True

        # Docling returned empty.
        if lw_raised is not None:
            # Lightweight had raised earlier — record lightweight failure.
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
        # Lightweight raised — record failure (no fallback available).
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

    for sha256, path, mime_type, size_bytes in batch:
        if not _is_allowlisted(mime_type, path, cfg):
            # Silently skip; not counted in touched.
            continue

        with conn.cursor() as cur:
            cur.execute("SAVEPOINT extract_blob")

        try:
            wrote = _process_blob(
                conn, sha256, path, mime_type, size_bytes, cfg, lw, dl
            )
            with conn.cursor() as cur:
                cur.execute("RELEASE SAVEPOINT extract_blob")
            # Count both successful writes and planned failures recorded
            # inside _process_blob (e.g. docling raised).
            touched += 1
        except Exception as exc:  # noqa: BLE001 — outer safety net
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT extract_blob")
            if _record_failure_safely(conn, sha256, "unexpected", exc):
                touched += 1

    conn.commit()
    return touched


def run_extract_worker(
    *,
    conn_factory: Callable[[], psycopg.Connection],
    cfg: SearchConfig,
    stop_event: threading.Event,
) -> None:
    """Background loop: drain the extraction queue, sleep, repeat.

    Opens a fresh connection via ``conn_factory`` for each outer iteration so
    that server-side idle timeouts and transaction-state leaks cannot
    accumulate across sweeps. Reconnects with exponential backoff (1 s → 60 s
    cap) when ``conn_factory`` raises.  Exits cleanly as soon as
    ``stop_event`` is set — including during the inter-sweep sleep so the
    thread joins promptly.

    Args:
        conn_factory: Zero-argument callable that returns a fresh
            ``psycopg.Connection``.  The worker owns the connection's
            lifecycle (close is called in ``finally``).
        cfg: ``SearchConfig`` supplying all tunables: poll interval, batch
            size, retry cap, and the master enable flag.
        stop_event: Set this to request graceful shutdown.  The loop checks
            the event before each sweep and uses ``stop_event.wait`` for
            inter-sweep sleeps so cancellation is near-instantaneous.
    """
    backoff = 1.0
    while not stop_event.is_set():
        try:
            conn = conn_factory()
        except Exception:
            _LOG.warning(
                "extract_worker: connect failed; backing off %.0fs", backoff
            )
            if stop_event.wait(timeout=backoff):
                return
            backoff = min(backoff * 2, 60.0)
            continue

        backoff = 1.0
        try:
            while not stop_event.is_set():
                touched = run_extract_worker_once(conn, cfg)
                if touched == 0:
                    break
            if stop_event.is_set():
                break
            if stop_event.wait(timeout=cfg.extract_worker_poll_interval_s):
                break
        except Exception:
            _LOG.exception("extract_worker: error during sweep")
            if stop_event.wait(timeout=backoff):
                return
            backoff = min(backoff * 2, 60.0)
        finally:
            try:
                conn.close()
            except Exception:
                pass
