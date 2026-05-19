"""Tests for extract_worker — text/empty/raised flow + SAVEPOINT discipline."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import psycopg
import pytest

from localmail.config import SearchConfig
from localmail.search.extract_worker import run_extract_worker, run_extract_worker_once
from tests.conftest import TEST_DSN


def _seed_blob(
    db_conn,
    content: bytes,
    mime_type: str,
    attachments_root: Path,
    filename: str = "att.bin",
) -> bytes:
    """Insert a blob row + write the bytes to disk; return sha256."""
    sha = hashlib.sha256(content).digest()
    sub = sha.hex()
    blob_path = attachments_root / "blobs" / sub[:2] / sub[2:4] / sub
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(content)

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, str(blob_path), mime_type, len(content)),
        )
    db_conn.commit()
    return sha


def test_extract_worker_processes_plain_text(db_conn, tmp_path) -> None:
    """A text/plain blob produces an attachment_text row with extractor='lightweight@1.0'."""
    sha = _seed_blob(
        db_conn, b"the quick brown fox", "text/plain", tmp_path, "a.txt"
    )
    cfg = SearchConfig()

    wrote = run_extract_worker_once(db_conn, cfg)

    assert wrote >= 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor, extracted_text FROM attachment_text "
            "WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
    assert row is not None
    extractor, text = row
    assert extractor == "lightweight@1.0"
    assert "the quick brown fox" in text


def test_extract_worker_skips_non_allowlist_blob(db_conn, tmp_path) -> None:
    """Blobs with MIME types outside the allowlist are silently skipped."""
    sha = _seed_blob(
        db_conn, b"\x00\x01\x02", "image/png", tmp_path, "logo.png"
    )
    cfg = SearchConfig()

    run_extract_worker_once(db_conn, cfg)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM attachment_text WHERE sha256 = %s", (sha,)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


def test_extract_worker_inserts_size_skipped_sentinel(db_conn, tmp_path) -> None:
    """Blobs exceeding extractor_max_blob_bytes get a 'size-skipped' sentinel row."""
    payload = b"x" * (1024 * 1024)
    cfg = SearchConfig(extractor_max_blob_bytes=100)
    sha = _seed_blob(db_conn, payload, "text/plain", tmp_path, "big.txt")

    run_extract_worker_once(db_conn, cfg)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor, extracted_text FROM attachment_text "
            "WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
    assert row == ("size-skipped", "")


def test_extract_worker_sentinel_for_lightweight_empty_non_pdf(
    db_conn, tmp_path
) -> None:
    """An empty text/plain blob gets a 'lightweight-empty' sentinel — no docling fallback."""
    sha = _seed_blob(db_conn, b"", "text/plain", tmp_path, "empty.txt")
    cfg = SearchConfig()

    run_extract_worker_once(db_conn, cfg)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor FROM attachment_text WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "lightweight-empty"


def test_extract_worker_records_failure_on_corrupt_pdf(
    db_conn, tmp_path
) -> None:
    """A corrupt PDF triggers ExtractorError; the blob lands in failed_extractions."""
    sha = _seed_blob(
        db_conn, b"%PDF-1.4\nthis is not a valid PDF body",
        "application/pdf", tmp_path, "corrupt.pdf",
    )
    cfg = SearchConfig()

    run_extract_worker_once(db_conn, cfg)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor, error_class FROM failed_extractions "
            "WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
    # lightweight raised → docling fallback attempted. If docling not
    # installed, ExtractorError ("not installed") from docling path.
    # Either way, failed_extractions has a row.
    assert row is not None
    assert row[0] in ("lightweight", "docling")


def test_extract_worker_batch_isolation_on_unexpected_exception(
    db_conn, tmp_path, monkeypatch
) -> None:
    """When _process_blob raises unexpectedly on blob A, blob B in the
    same batch still gets processed. Verifies SAVEPOINT discipline."""
    import localmail.search.extract_worker as ew_mod

    sha_a = _seed_blob(db_conn, b"poison", "text/plain", tmp_path, "a.txt")
    sha_b = _seed_blob(db_conn, b"good text", "text/plain", tmp_path, "b.txt")
    cfg = SearchConfig()

    real_process = ew_mod._process_blob

    def _maybe_poison(conn, sha256, *args, **kwargs):
        if sha256 == sha_a:
            raise RuntimeError("synthetic poison")
        return real_process(conn, sha256, *args, **kwargs)

    monkeypatch.setattr(ew_mod, "_process_blob", _maybe_poison)

    ew_mod.run_extract_worker_once(db_conn, cfg)

    # Blob A should land in failed_extractions with extractor="unexpected".
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor FROM failed_extractions WHERE sha256 = %s",
            (sha_a,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "unexpected"

    # Blob B should still have an attachment_text row.
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extracted_text FROM attachment_text WHERE sha256 = %s",
            (sha_b,),
        )
        row = cur.fetchone()
        assert row is not None
        assert "good text" in row[0]


def test_extract_worker_excludes_blobs_at_max_retries(
    db_conn, tmp_path
) -> None:
    """When retry_count >= max_retries, the blob is excluded from the batch."""
    sha = _seed_blob(
        db_conn, b"%PDF-1.4\nstill broken",
        "application/pdf", tmp_path, "broken.pdf",
    )
    # Pre-seed the failure row at max-retries.
    cfg = SearchConfig(extract_worker_max_retries=2)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO failed_extractions "
            "(sha256, extractor, error_class, error_message, retry_count) "
            "VALUES (%s, 'lightweight', 'X', 'X', %s)",
            (sha, cfg.extract_worker_max_retries),
        )
    db_conn.commit()

    wrote = run_extract_worker_once(db_conn, cfg)
    # The blob should NOT be re-tried.
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT retry_count FROM failed_extractions WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == cfg.extract_worker_max_retries  # unchanged


def test_run_extract_worker_drains_queue_then_idles(db_conn, tmp_path) -> None:
    """run_extract_worker drains pending work, then blocks on the poll
    interval until the stop event is set."""
    _seed_blob(db_conn, b"blob alpha content", "text/plain", tmp_path, "a.txt")
    _seed_blob(db_conn, b"blob beta content", "text/plain", tmp_path, "b.txt")

    cfg = SearchConfig(extract_worker_poll_interval_s=1)
    stop = threading.Event()

    def _make_conn() -> psycopg.Connection:
        return psycopg.connect(TEST_DSN)

    t = threading.Thread(
        target=run_extract_worker,
        kwargs={"conn_factory": _make_conn, "cfg": cfg, "stop_event": stop},
        daemon=True,
    )
    t.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM attachment_text")
            row = cur.fetchone()
            assert row is not None
            if row[0] >= 2:
                break
        time.sleep(0.1)

    stop.set()
    t.join(timeout=3)
    assert not t.is_alive()

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attachment_text")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 2


def test_run_extract_worker_stops_immediately_when_event_set(db_conn) -> None:
    """Setting stop_event before starting the loop causes immediate exit."""
    cfg = SearchConfig(extract_worker_poll_interval_s=30)
    stop = threading.Event()
    stop.set()  # already set before the loop starts

    def _make_conn() -> psycopg.Connection:
        return psycopg.connect(TEST_DSN)

    start = time.monotonic()
    run_extract_worker(conn_factory=_make_conn, cfg=cfg, stop_event=stop)
    elapsed = time.monotonic() - start

    # Should exit in <1s, well under the 30s poll interval.
    assert elapsed < 1.0


# --- Transient vs poison-pill classification (#36) ---------------------------
#
# The extract_worker must distinguish transient extractor errors (network
# blips, OOM, model-download timeouts) from genuine poison-pill blobs
# (corrupt bytes, encrypted, MIME mismatch). Transient errors must NOT be
# recorded in failed_extractions — the blob should remain eligible for the
# next sweep with its retry_count intact. Poison-pills continue to follow
# the existing _record_failure_safely path so they're permanently skipped
# after extract_worker_max_retries.


def test_is_transient_recognises_transient_extractor_error() -> None:
    """A TransientExtractorError instance is unconditionally transient."""
    from localmail.search.extract_worker import _is_transient
    from localmail.search.extractor import TransientExtractorError

    assert _is_transient(TransientExtractorError("simulated blip"))


def test_is_transient_recognises_connection_error_in_cause_chain() -> None:
    """An ExtractorError caused by ConnectionError (docling model fetch
    blip) walks its cause chain and is recognised as transient."""
    from localmail.search.extract_worker import _is_transient
    from localmail.search.extractor import ExtractorError

    try:
        try:
            raise ConnectionError("dns refused")
        except ConnectionError as inner:
            raise ExtractorError("docling wrap") from inner
    except ExtractorError as exc:
        assert _is_transient(exc)


def test_is_transient_recognises_timeout_error_in_cause_chain() -> None:
    """TimeoutError anywhere in the cause chain is transient."""
    from localmail.search.extract_worker import _is_transient
    from localmail.search.extractor import ExtractorError

    try:
        try:
            raise TimeoutError("read timed out")
        except TimeoutError as inner:
            raise ExtractorError("docling wrap") from inner
    except ExtractorError as exc:
        assert _is_transient(exc)


def test_is_transient_recognises_memory_error() -> None:
    """A raw MemoryError is transient (OOM resolves once other procs free)."""
    from localmail.search.extract_worker import _is_transient

    assert _is_transient(MemoryError("oom"))


def test_is_transient_rejects_value_error() -> None:
    """ValueError is a permanent poison-pill class (parse failures, etc.)."""
    from localmail.search.extract_worker import _is_transient

    assert not _is_transient(ValueError("malformed bytes"))


def test_is_transient_rejects_plain_extractor_error() -> None:
    """A bare ExtractorError without a transient cause chain is permanent."""
    from localmail.search.extract_worker import _is_transient
    from localmail.search.extractor import ExtractorError

    assert not _is_transient(ExtractorError("pypdf: malformed PDF"))


def test_extract_worker_does_not_record_transient_error(
    db_conn, tmp_path, monkeypatch
) -> None:
    """An extractor raising TransientExtractorError must NOT produce a
    failed_extractions row — the blob remains eligible for retry."""
    from localmail.search.extractor import (
        LightweightExtractor,
        TransientExtractorError,
    )

    sha = _seed_blob(db_conn, b"text content", "text/plain", tmp_path, "a.txt")

    def _fail_transient(self, blob_path, mime_type):
        raise TransientExtractorError("simulated network blip")

    monkeypatch.setattr(LightweightExtractor, "extract", _fail_transient)

    run_extract_worker_once(db_conn, SearchConfig())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM failed_extractions WHERE sha256 = %s",
            (sha,),
        )
        row_failed = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM attachment_text WHERE sha256 = %s",
            (sha,),
        )
        row_text = cur.fetchone()

    assert row_failed is not None and row_failed[0] == 0
    assert row_text is not None and row_text[0] == 0


def test_extract_worker_does_not_record_connection_error(
    db_conn, tmp_path, monkeypatch
) -> None:
    """An ExtractorError caused by ConnectionError (docling model fetch
    blip) is classified transient and not recorded."""
    from localmail.search.extractor import (
        ExtractorError,
        LightweightExtractor,
    )

    sha = _seed_blob(db_conn, b"text content", "text/plain", tmp_path, "a.txt")

    def _fail_with_connection_error(self, blob_path, mime_type):
        try:
            raise ConnectionError("model fetch failed")
        except ConnectionError as inner:
            raise ExtractorError("wrapped") from inner

    monkeypatch.setattr(
        LightweightExtractor, "extract", _fail_with_connection_error
    )

    run_extract_worker_once(db_conn, SearchConfig())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM failed_extractions WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == 0


def test_extract_worker_records_permanent_extractor_error(
    db_conn, tmp_path, monkeypatch
) -> None:
    """Regression: a plain ExtractorError (no transient cause) IS still
    recorded as a poison-pill in failed_extractions."""
    from localmail.search.extractor import (
        ExtractorError,
        LightweightExtractor,
    )

    sha = _seed_blob(db_conn, b"text content", "text/plain", tmp_path, "a.txt")

    def _fail_permanently(self, blob_path, mime_type):
        raise ExtractorError("pypdf: bad header")

    monkeypatch.setattr(LightweightExtractor, "extract", _fail_permanently)

    run_extract_worker_once(db_conn, SearchConfig())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extractor, error_class FROM failed_extractions "
            "WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "lightweight"
    assert row[1] == "ExtractorError"


def test_extract_worker_transient_does_not_poison_batch(
    db_conn, tmp_path, monkeypatch
) -> None:
    """A transient failure on blob A must not block blob B in the same
    batch from being processed. Mirrors the batch_isolation guarantee
    that the existing poison-pill path already provides."""
    from localmail.search.extractor import (
        LightweightExtractor,
        TransientExtractorError,
    )

    sha_a = _seed_blob(
        db_conn, b"fragile network blob", "text/plain", tmp_path, "a.txt"
    )
    sha_b = _seed_blob(
        db_conn, b"healthy second blob", "text/plain", tmp_path, "b.txt"
    )

    real_extract = LightweightExtractor.extract

    def _maybe_transient(self, blob_path, mime_type):
        if blob_path.name == sha_a.hex():
            raise TransientExtractorError("simulated blip")
        return real_extract(self, blob_path, mime_type)

    monkeypatch.setattr(LightweightExtractor, "extract", _maybe_transient)

    run_extract_worker_once(db_conn, SearchConfig())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM failed_extractions WHERE sha256 = %s",
            (sha_a,),
        )
        a_fail = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM attachment_text WHERE sha256 = %s",
            (sha_a,),
        )
        a_text = cur.fetchone()
        cur.execute(
            "SELECT extracted_text FROM attachment_text WHERE sha256 = %s",
            (sha_b,),
        )
        b_row = cur.fetchone()

    assert a_fail is not None and a_fail[0] == 0
    assert a_text is not None and a_text[0] == 0
    assert b_row is not None and "healthy second blob" in b_row[0]


def test_extract_worker_transient_blob_eligible_next_sweep(
    db_conn, tmp_path, monkeypatch
) -> None:
    """After a transient error, the next sweep can pick the same blob up
    again (no failed_extractions row gating it out)."""
    from localmail.search.extractor import (
        LightweightExtractor,
        TransientExtractorError,
    )

    sha = _seed_blob(
        db_conn, b"will recover", "text/plain", tmp_path, "a.txt"
    )

    real_extract = LightweightExtractor.extract
    calls = {"n": 0}

    def _flaky(self, blob_path, mime_type):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientExtractorError("first call blips")
        return real_extract(self, blob_path, mime_type)

    monkeypatch.setattr(LightweightExtractor, "extract", _flaky)

    # First sweep: transient error.
    run_extract_worker_once(db_conn, SearchConfig())
    # Second sweep: should succeed because the blob isn't gated by any
    # failed_extractions row.
    run_extract_worker_once(db_conn, SearchConfig())

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extracted_text FROM attachment_text WHERE sha256 = %s",
            (sha,),
        )
        row = cur.fetchone()
    assert row is not None
    assert "will recover" in row[0]
    assert calls["n"] >= 2
