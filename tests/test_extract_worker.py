"""Tests for extract_worker — text/empty/raised flow + SAVEPOINT discipline."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from localmail.config import SearchConfig
from localmail.search.extract_worker import run_extract_worker_once


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
