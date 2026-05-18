import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from localmail.api.attachments import (
    get_attachment_metadata,
    get_attachment_text,
    open_attachment_bytes,
)
from localmail.api.errors import NotFound, ValidationFailed

_ANY_ACCOUNT = list(range(1, 1000))


def _seed_account_and_carrier(
    conn: psycopg.Connection, sha_hex: str, filename: str = "x.pdf",
) -> int:
    """Insert an account + a message that carries the blob, so the ACL check
    finds an allowed message and returns True. Returns the account_id.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('acct', 'h@example.com', 'imap.example.com', 'password') "
            "RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        aid = int(row[0])
        raw = b"From: x\r\nSubject: carrier\r\n\r\nx"
        cur.execute(
            "INSERT INTO messages "
            "(account_id, message_id, raw_bytes, raw_sha256, size_bytes, "
            " subject, body_text, headers, attachments, date_sent) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)",
            (
                aid, f"<{sha_hex}@x>", raw, hashlib.sha256(raw).digest(), len(raw),
                "carrier", "x", "{}",
                psycopg.types.json.Jsonb([{"filename": filename, "sha256": sha_hex}]),
                datetime.now(timezone.utc),
            ),
        )
    return aid


def _seed_blob(conn: psycopg.Connection, tmp_path: Path, sha_hex: str, payload: bytes,
               mime: str = "application/pdf") -> int:
    """Seed blob row + on-disk file + carrying message + account. Returns account_id."""
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
    return _seed_account_and_carrier(conn, sha_hex)


def test_get_attachment_metadata_returns_mime_size_path(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    sha = "ab" * 32
    aid = _seed_blob(db_conn, tmp_path, sha, b"%PDF-1.4 hello")
    db_conn.commit()
    meta = get_attachment_metadata(db_conn, sha, allowed_account_ids=[aid])
    assert meta["sha256"] == sha
    assert meta["mime_type"] == "application/pdf"
    assert meta["size_bytes"] == len(b"%PDF-1.4 hello")


def test_get_attachment_metadata_not_found(db_conn: psycopg.Connection) -> None:
    with pytest.raises(NotFound):
        get_attachment_metadata(db_conn, "00" * 32, allowed_account_ids=_ANY_ACCOUNT)


def test_open_attachment_bytes_returns_path_and_size(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    sha = "cd" * 32
    payload = b"hello bytes"
    aid = _seed_blob(db_conn, tmp_path, sha, payload)
    db_conn.commit()
    f, mime, size = open_attachment_bytes(db_conn, sha, allowed_account_ids=[aid])
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
    aid = _seed_account_and_carrier(db_conn, sha)
    db_conn.commit()
    os.remove(bad_path)
    with pytest.raises(NotFound):
        open_attachment_bytes(db_conn, sha, allowed_account_ids=[aid])


def test_get_attachment_text_returns_extracted(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    sha = "12" * 32
    aid = _seed_blob(db_conn, tmp_path, sha, b"%PDF")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) VALUES (%s, %s, %s)",
            (bytes.fromhex(sha), "pypdf", "Hello world"),
        )
    db_conn.commit()
    text = get_attachment_text(db_conn, sha, allowed_account_ids=[aid])
    assert text == "Hello world"


def test_get_attachment_text_not_extracted(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    sha = "34" * 32
    aid = _seed_blob(db_conn, tmp_path, sha, b"%PDF")
    db_conn.commit()
    with pytest.raises(NotFound):
        get_attachment_text(db_conn, sha, allowed_account_ids=[aid])


@pytest.mark.parametrize("bad_sha", [
    "not-hex",
    "",
    "ab" * 31,
    "ab" * 33,
    "zz" * 32,
])
def test_malformed_sha256_raises_validation(db_conn: psycopg.Connection, bad_sha: str) -> None:
    """A non-hex or wrong-length sha256 path param surfaces as 400, not 500.

    Without this, a request like GET /v1/attachments/foo would crash with
    bytes.fromhex ValueError and the user would see an opaque 500.
    """
    for fn in (get_attachment_metadata, get_attachment_text, open_attachment_bytes):
        with pytest.raises(ValidationFailed):
            fn(db_conn, bad_sha, allowed_account_ids=_ANY_ACCOUNT)
