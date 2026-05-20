"""Attachment metadata, streaming, and extracted-text accessors.

Attachment blobs are content-addressable and shared across messages and
accounts. A caller is permitted to read a blob iff **any** message in an
ACL-allowed account references the blob's sha256 in its `attachments` JSONB.
The check is done at the SQL boundary via an EXISTS subquery against
`messages.attachments @> jsonb_build_array(jsonb_build_object('sha256', %s))`,
which is accelerated by the GIN index from migration 0013.
"""
from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

import psycopg

from localmail.api.errors import NotFound, ValidationFailed


def _parse_sha256_hex(sha256_hex: str) -> bytes:
    """Decode a 64-char hex string to 32 bytes. Raises ValidationFailed on bad input.

    Used at every API entry point so malformed path parameters surface as
    400 problem+json rather than an unhandled 500 from bytes.fromhex.
    """
    if not isinstance(sha256_hex, str) or len(sha256_hex) != 64:
        raise ValidationFailed(
            f"sha256 must be a 64-character hex string, got {len(sha256_hex)} chars"
        )
    try:
        return bytes.fromhex(sha256_hex)
    except ValueError as exc:
        raise ValidationFailed(f"sha256 is not valid hex: {exc}") from exc


def _caller_can_read_blob(
    conn: psycopg.Connection, sha256_hex: str, allowed_account_ids: list[int],
) -> bool:
    """True iff some message in an allowed account references the blob.

    The check uses the same JSONB containment + GIN-indexed predicate that
    arm 4 of search uses, so it is fast even for popular blobs.
    """
    if not allowed_account_ids:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM messages "
            "WHERE account_id = ANY(%s) "
            "  AND attachments @> jsonb_build_array("
            "        jsonb_build_object('sha256', %s::text)"
            "      ) "
            "LIMIT 1",
            (allowed_account_ids, sha256_hex),
        )
        return cur.fetchone() is not None


def get_attachment_metadata(
    conn: psycopg.Connection, sha256_hex: str, *, allowed_account_ids: list[int],
) -> dict[str, object]:
    """Return {sha256, mime_type, size_bytes} for a blob. Raises NotFound."""
    sha_bytes = _parse_sha256_hex(sha256_hex)
    if not _caller_can_read_blob(conn, sha256_hex, allowed_account_ids):
        raise NotFound(f"attachment {sha256_hex} not found")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mime_type, size_bytes FROM attachment_blobs WHERE sha256 = %s",
            (sha_bytes,),
        )
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"attachment {sha256_hex} not found")
    return {
        "sha256": sha256_hex,
        "mime_type": row[0],
        "size_bytes": int(row[1]),
    }


def get_attachment_blob_info(
    conn: psycopg.Connection, sha256_hex: str, *, allowed_account_ids: list[int],
) -> tuple[str, int]:
    """Return ``(mime_type, size_bytes)`` for a blob — DB-only, no file open.

    Lightweight probe used by the streaming route before evaluating the
    conditional-GET preconditions (#62). It enforces the same ACL check
    as ``open_attachment_bytes`` so a 404 still wins over a 304 for
    callers who cannot read the blob, but it deliberately skips the
    ``Path.exists()`` / file-open work and the JSONB filename scan that
    are wasted when ``If-None-Match`` is going to short-circuit to 304.

    Raises ``NotFound`` if the blob row is missing or the caller's ACL
    does not include any account that references the blob.
    """
    sha_bytes = _parse_sha256_hex(sha256_hex)
    if not _caller_can_read_blob(conn, sha256_hex, allowed_account_ids):
        raise NotFound(f"attachment {sha256_hex} not found")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mime_type, size_bytes FROM attachment_blobs WHERE sha256 = %s",
            (sha_bytes,),
        )
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"attachment {sha256_hex} not found")
    return row[0], int(row[1])


def open_attachment_bytes(
    conn: psycopg.Connection, sha256_hex: str, *, allowed_account_ids: list[int],
) -> tuple[BinaryIO, str, int]:
    """Open the blob file for streaming. Returns (file, mime_type, size).

    Caller closes the file. Raises NotFound if the DB row is missing, the
    on-disk file is missing, or the caller's ACL does not include any
    account that references the blob.
    """
    sha_bytes = _parse_sha256_hex(sha256_hex)
    if not _caller_can_read_blob(conn, sha256_hex, allowed_account_ids):
        raise NotFound(f"attachment {sha256_hex} not found")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mime_type, size_bytes, path FROM attachment_blobs WHERE sha256 = %s",
            (sha_bytes,),
        )
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"attachment {sha256_hex} not found")
    mime, size, path = row
    p = Path(path)
    if not p.exists():
        raise NotFound(f"attachment {sha256_hex} file missing at {path}")
    return p.open("rb"), mime, int(size)


def get_attachment_filename(
    conn: psycopg.Connection, sha256_hex: str, *, allowed_account_ids: list[int],
) -> str | None:
    """Return the original per-message filename for a blob, or None.

    Blobs are content-addressable and may be referenced by multiple messages
    (potentially with different filenames). We pick the earliest carrying
    message in any ACL-allowed account — `ORDER BY messages.id LIMIT 1` —
    so the choice is deterministic and the same blob always serves the same
    download name to a given user. Returns None when no allowed message
    references the blob, or when the message JSONB has no 'filename' key.
    The route layer turns None into a generic sha-prefix fallback.
    """
    _parse_sha256_hex(sha256_hex)
    if not allowed_account_ids:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a->>'filename' "
            "FROM ("
            "  SELECT id, attachments FROM messages "
            "  WHERE account_id = ANY(%s) "
            "    AND attachments @> jsonb_build_array("
            "          jsonb_build_object('sha256', %s::text)"
            "        ) "
            "  ORDER BY id ASC LIMIT 1"
            ") m, jsonb_array_elements(m.attachments) AS a "
            "WHERE a->>'sha256' = %s "
            "LIMIT 1",
            (allowed_account_ids, sha256_hex, sha256_hex),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def get_attachment_text(
    conn: psycopg.Connection, sha256_hex: str, *, allowed_account_ids: list[int],
) -> str:
    """Return extracted text for a blob. Raises NotFound if not yet extracted
    or if the caller cannot read any carrying message.
    """
    sha_bytes = _parse_sha256_hex(sha256_hex)
    if not _caller_can_read_blob(conn, sha256_hex, allowed_account_ids):
        raise NotFound(f"no extracted text for attachment {sha256_hex}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT extracted_text FROM attachment_text WHERE sha256 = %s",
            (sha_bytes,),
        )
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"no extracted text for attachment {sha256_hex}")
    return row[0]
