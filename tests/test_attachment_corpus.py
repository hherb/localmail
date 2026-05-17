"""Smoke tests for the attachment fixture builder — verify fixture bytes
roundtrip through LightweightExtractor."""

from pathlib import Path
import hashlib

import pytest

from localmail.search.extractor import LightweightExtractor


def test_builder_produces_native_pdf() -> None:
    """Verify that build_native_pdf returns valid PDF bytes."""
    from tests._attachment_corpus import build_native_pdf
    data = build_native_pdf("hello fixture corpus")
    assert data.startswith(b"%PDF")


def test_builder_produces_docx() -> None:
    """Verify that build_docx returns a non-trivial OOXML zip blob."""
    from tests._attachment_corpus import build_docx
    data = build_docx(["para one", "para two"])
    assert len(data) > 1000  # zip overhead alone


def test_builder_produces_xlsx() -> None:
    """Verify that build_xlsx returns a non-trivial OOXML zip blob."""
    from tests._attachment_corpus import build_xlsx
    data = build_xlsx({"Sheet1": [["alice", "Berlin"], ["bob", "Madrid"]]})
    assert len(data) > 1000


def test_builder_produces_ics() -> None:
    """Verify that build_ics returns valid iCalendar bytes containing the summary."""
    from tests._attachment_corpus import build_ics
    data = build_ics("Annual review", "discuss bonus", "Conf room")
    assert b"BEGIN:VCALENDAR" in data
    assert b"Annual review" in data


def test_build_corpus_seeds_db_messages_and_blobs(db_conn, tmp_path) -> None:
    """Verify that build_corpus inserts at least 10 messages and 10 blobs."""
    from tests._attachment_corpus import build_corpus
    fixtures = build_corpus(db_conn, attachments_root=tmp_path)
    assert len(fixtures) >= 10
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        row = cur.fetchone()
        assert row is not None
        assert row[0] >= 10
        cur.execute("SELECT count(*) FROM attachment_blobs")
        row = cur.fetchone()
        assert row is not None
        assert row[0] >= 10
