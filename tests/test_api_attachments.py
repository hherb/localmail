import os
from pathlib import Path

import psycopg
import pytest

from localmail.api.attachments import (
    get_attachment_metadata,
    get_attachment_text,
    open_attachment_bytes,
)
from localmail.api.errors import NotFound


def _seed_blob(conn: psycopg.Connection, tmp_path: Path, sha_hex: str, payload: bytes,
               mime: str = "application/pdf") -> None:
    fanout = tmp_path / "blobs" / sha_hex[:2] / sha_hex[2:4]
    fanout.mkdir(parents=True, exist_ok=True)
    blob_path = fanout / sha_hex
    blob_path.write_bytes(payload)
    sha = bytes.fromhex(sha_hex)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
            "VALUES (%s, %s, %s, %s)",
            (sha, mime, len(payload), str(blob_path)),
        )


def test_get_attachment_metadata_returns_mime_size_path(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    sha = "ab" * 32
    _seed_blob(db_conn, tmp_path, sha, b"%PDF-1.4 hello")
    db_conn.commit()
    meta = get_attachment_metadata(db_conn, sha)
    assert meta["sha256"] == sha
    assert meta["mime_type"] == "application/pdf"
    assert meta["size_bytes"] == len(b"%PDF-1.4 hello")


def test_get_attachment_metadata_not_found(db_conn: psycopg.Connection) -> None:
    with pytest.raises(NotFound):
        get_attachment_metadata(db_conn, "00" * 32)


def test_open_attachment_bytes_returns_path_and_size(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    sha = "cd" * 32
    payload = b"hello bytes"
    _seed_blob(db_conn, tmp_path, sha, payload)
    db_conn.commit()
    f, mime, size = open_attachment_bytes(db_conn, sha)
    try:
        assert mime == "application/pdf"
        assert size == len(payload)
        assert f.read() == payload
    finally:
        f.close()


def test_open_attachment_bytes_missing_file(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    sha = "ef" * 32
    fanout = tmp_path / "blobs" / sha[:2] / sha[2:4]
    fanout.mkdir(parents=True, exist_ok=True)
    bad_path = fanout / sha
    bad_path.write_bytes(b"x")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
            "VALUES (%s, %s, %s, %s)",
            (bytes.fromhex(sha), "application/octet-stream", 1, str(bad_path)),
        )
    db_conn.commit()
    os.remove(bad_path)
    with pytest.raises(NotFound):
        open_attachment_bytes(db_conn, sha)


def test_get_attachment_text_returns_extracted(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    sha = "12" * 32
    _seed_blob(db_conn, tmp_path, sha, b"%PDF")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) VALUES (%s, %s, %s)",
            (bytes.fromhex(sha), "pypdf", "Hello world"),
        )
    db_conn.commit()
    text = get_attachment_text(db_conn, sha)
    assert text == "Hello world"


def test_get_attachment_text_not_extracted(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    sha = "34" * 32
    _seed_blob(db_conn, tmp_path, sha, b"%PDF")
    db_conn.commit()
    with pytest.raises(NotFound):
        get_attachment_text(db_conn, sha)
