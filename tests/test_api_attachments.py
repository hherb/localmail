# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import hashlib
import inspect
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from localmail.api.attachments import (
    _open_blob_file_at,
    get_attachment_blob_info,
    get_attachment_filename,
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
    for fn in (
        get_attachment_blob_info,
        get_attachment_metadata,
        get_attachment_text,
        open_attachment_bytes,
        get_attachment_filename,
    ):
        with pytest.raises(ValidationFailed):
            fn(db_conn, bad_sha, allowed_account_ids=_ANY_ACCOUNT)


def test_get_attachment_filename_returns_jsonb_filename(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    sha = "5a" * 32
    aid = _seed_blob(db_conn, tmp_path, sha, b"%PDF", )
    db_conn.commit()
    assert get_attachment_filename(db_conn, sha, allowed_account_ids=[aid]) == "x.pdf"


def test_get_attachment_filename_returns_none_when_no_acl_match(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    sha = "5b" * 32
    _seed_blob(db_conn, tmp_path, sha, b"%PDF")
    db_conn.commit()
    assert get_attachment_filename(db_conn, sha, allowed_account_ids=[]) is None
    assert get_attachment_filename(db_conn, sha, allowed_account_ids=[99999]) is None


def test_get_attachment_filename_returns_none_when_jsonb_has_no_filename(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    """If the attachments JSONB row lacks a 'filename' key, return None."""
    sha = "5c" * 32
    fanout = tmp_path / "blobs" / sha[:2] / sha[2:4]
    fanout.mkdir(parents=True, exist_ok=True)
    blob_path = fanout / sha
    blob_path.write_bytes(b"x")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
            "VALUES (%s, %s, %s, %s)",
            (bytes.fromhex(sha), "application/octet-stream", 1, str(blob_path)),
        )
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('a', 'h@e.com', 'imap.e.com', 'password') RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        aid = int(row[0])
        raw = b"x"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)",
            (
                aid, f"<{sha}@x>", raw, hashlib.sha256(raw).digest(), 1, "{}",
                psycopg.types.json.Jsonb([{"sha256": sha}]),
                datetime.now(timezone.utc),
            ),
        )
    db_conn.commit()
    assert get_attachment_filename(db_conn, sha, allowed_account_ids=[aid]) is None


def test_get_attachment_filename_prefers_first_carrying_message(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    """When multiple messages carry the same blob with different filenames,
    return a deterministic pick (the earliest-inserted carrier)."""
    sha = "5d" * 32
    aid = _seed_blob(db_conn, tmp_path, sha, b"%PDF")
    with db_conn.cursor() as cur:
        raw = b"second"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)",
            (
                aid, f"<second-{sha}@x>", raw, hashlib.sha256(raw).digest(), len(raw),
                "{}",
                psycopg.types.json.Jsonb([{"filename": "renamed.pdf", "sha256": sha}]),
                datetime.now(timezone.utc),
            ),
        )
    db_conn.commit()
    assert get_attachment_filename(db_conn, sha, allowed_account_ids=[aid]) == "x.pdf"


def test_get_attachment_blob_info_returns_mime_size_and_path(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    """Cheap probe used before the conditional-GET 304 short-circuit (#62)
    returns ``(mime, size, path)``. Path is included so the body-carrying
    200/206 path can hand it straight to ``_open_blob_file_at`` and avoid
    a duplicate DB roundtrip (#64, #67)."""
    sha = "60" * 32
    aid = _seed_blob(db_conn, tmp_path, sha, b"%PDF-1.4 probe")
    db_conn.commit()
    mime, size, path = get_attachment_blob_info(
        db_conn, sha, allowed_account_ids=[aid],
    )
    assert mime == "application/pdf"
    assert size == len(b"%PDF-1.4 probe")
    assert path == str(tmp_path / "blobs" / sha[:2] / sha[2:4] / sha)


def test_get_attachment_blob_info_not_found(db_conn: psycopg.Connection) -> None:
    with pytest.raises(NotFound):
        get_attachment_blob_info(db_conn, "00" * 32, allowed_account_ids=_ANY_ACCOUNT)


def test_get_attachment_blob_info_acl_deny(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    """ACL must run first — an empty grant list yields NotFound, never a
    leak that the sha exists. Underpins the #62 acceptance criterion that
    a 404 still wins over a 304 for unauthorised users."""
    sha = "61" * 32
    _seed_blob(db_conn, tmp_path, sha, b"%PDF")
    db_conn.commit()
    with pytest.raises(NotFound):
        get_attachment_blob_info(db_conn, sha, allowed_account_ids=[])
    with pytest.raises(NotFound):
        get_attachment_blob_info(db_conn, sha, allowed_account_ids=[99999])


def test_get_attachment_blob_info_does_not_touch_filesystem(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    """Probe must succeed even if the on-disk blob has been deleted —
    that is the cost-saving over `open_attachment_bytes` and is the
    whole reason #62 introduces a separate probe function."""
    sha = "62" * 32
    aid = _seed_blob(db_conn, tmp_path, sha, b"%PDF-1.4 probe")
    db_conn.commit()
    blob_path = tmp_path / "blobs" / sha[:2] / sha[2:4] / sha
    blob_path.unlink()
    mime, size, _path = get_attachment_blob_info(
        db_conn, sha, allowed_account_ids=[aid],
    )
    assert mime == "application/pdf"
    assert size == len(b"%PDF-1.4 probe")


def test_get_attachment_blob_info_returns_path(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    """#64: the probe returns (mime, size, path) so the route can hand
    it straight to ``_open_blob_file_at``, eliminating the duplicate
    SELECT on the 200/206 body-carrying path (#67 replaced the prior
    ``open_attachment_bytes(..., prefetched=)`` mechanism with a
    distinct helper that takes no ``conn``, so the ACL bypass can't
    happen by accident). Path is `SELECT`ed alongside (mime, size) at
    zero extra cost — same row, same primary-key lookup."""
    sha = "70" * 32
    aid = _seed_blob(db_conn, tmp_path, sha, b"%PDF-1.4 probe")
    db_conn.commit()
    mime, size, path = get_attachment_blob_info(
        db_conn, sha, allowed_account_ids=[aid],
    )
    assert mime == "application/pdf"
    assert size == len(b"%PDF-1.4 probe")
    assert path == str(tmp_path / "blobs" / sha[:2] / sha[2:4] / sha)


def test_open_blob_file_at_opens_known_path(tmp_path: Path) -> None:
    """#64 / #67: the ACL-free file-open helper opens a known on-disk path
    without any DB roundtrip. The probe (``get_attachment_blob_info``) is
    the boundary; this helper just turns the cleared path into a file
    handle. Replaces the prior ``open_attachment_bytes(..., prefetched=)``
    kwarg, which was a footgun (no SQL-level guard against accidental
    ACL bypass by a future caller)."""
    sha = "71" * 32
    fanout = tmp_path / "blobs" / sha[:2] / sha[2:4]
    fanout.mkdir(parents=True, exist_ok=True)
    blob_path = fanout / sha
    payload = b"hello blob"
    blob_path.write_bytes(payload)
    fp = _open_blob_file_at(str(blob_path), sha)
    try:
        assert fp.read() == payload
    finally:
        fp.close()


def test_open_blob_file_at_missing_file_raises_not_found(tmp_path: Path) -> None:
    """#64 / #67: the file-existence check stays in the helper so a blob
    deleted between probe and open surfaces as NotFound rather than a
    confusing FileNotFoundError mid-stream."""
    sha = "72" * 32
    bad_path = tmp_path / "blobs" / sha[:2] / sha[2:4] / sha
    with pytest.raises(NotFound):
        _open_blob_file_at(str(bad_path), sha)


def test_open_attachment_bytes_has_no_prefetched_kwarg() -> None:
    """#67 acceptance: no public-API footgun. The prior ``prefetched=``
    kwarg let a caller skip the ACL EXISTS predicate with no SQL-level
    guard — anyone copy-pasting from the route would have silently
    bypassed ACL. The split into ``_open_blob_file_at`` removes the
    kwarg entirely; safe-by-default for every caller."""
    sig = inspect.signature(open_attachment_bytes)
    assert "prefetched" not in sig.parameters, (
        "open_attachment_bytes must not expose a prefetched= kwarg — "
        "see #67. Use the ACL-cleared probe + _open_blob_file_at pair "
        "in the route instead."
    )
