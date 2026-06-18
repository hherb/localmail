# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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
    conn: psycopg.Connection, sha256_hex: str, *, allowed_account_ids: list[int],
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


def _lookup_blob_row(
    conn: psycopg.Connection, sha256_hex: str, *, allowed_account_ids: list[int],
) -> tuple[str, int, str]:
    """Parse sha + ACL check + SELECT ``(mime_type, size_bytes, path)``.

    Single source of truth for every blob-row accessor. Path is selected
    alongside mime/size at zero extra cost — same row, same PK lookup —
    so callers that don't need it (metadata, probe) simply discard it,
    while ``open_attachment_bytes`` can reuse the helper instead of
    re-running ACL + SELECT.

    Raises ``NotFound`` if the row is missing or the caller's ACL does
    not include any account that references the blob.
    """
    sha_bytes = _parse_sha256_hex(sha256_hex)
    if not _caller_can_read_blob(
        conn, sha256_hex, allowed_account_ids=allowed_account_ids,
    ):
        raise NotFound(f"attachment {sha256_hex} not found")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mime_type, size_bytes, path FROM attachment_blobs "
            "WHERE sha256 = %s",
            (sha_bytes,),
        )
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"attachment {sha256_hex} not found")
    return row[0], int(row[1]), row[2]


def get_attachment_metadata(
    conn: psycopg.Connection, sha256_hex: str, *, allowed_account_ids: list[int],
) -> dict[str, object]:
    """Return {sha256, mime_type, size_bytes} for a blob. Raises NotFound."""
    mime, size, _path = _lookup_blob_row(
        conn, sha256_hex, allowed_account_ids=allowed_account_ids,
    )
    return {"sha256": sha256_hex, "mime_type": mime, "size_bytes": size}


def get_attachment_blob_info(
    conn: psycopg.Connection, sha256_hex: str, *, allowed_account_ids: list[int],
) -> tuple[str, int, str]:
    """Return ``(mime_type, size_bytes, path)`` for a blob — DB-only, no file open.

    Lightweight probe used by the streaming route before evaluating the
    conditional-GET preconditions (#62). Enforces the same ACL check as
    ``open_attachment_bytes`` so a 404 still wins over a 304 for callers
    who cannot read the blob, but skips the ``Path.exists()`` / file-open
    work and the JSONB filename scan that are wasted when
    ``If-None-Match`` is going to short-circuit to 304.

    Returns ``path`` so the route — once the probe has cleared ACL — can
    hand it to :func:`_open_blob_file_at` on the body-carrying 200/206
    path, collapsing the previous probe+open duplicate ACL + SELECT to a
    single pair (#64).

    Raises ``NotFound`` if the blob row is missing or the caller's ACL
    does not include any account that references the blob.
    """
    return _lookup_blob_row(
        conn, sha256_hex, allowed_account_ids=allowed_account_ids,
    )


def _open_blob_file_at(path: str, sha256_hex: str) -> BinaryIO:
    """Open a known blob path for streaming. **No ACL check — caller is the boundary.**

    Module-private helper used after an ACL-cleared probe
    (:func:`get_attachment_blob_info`) on the body-carrying 200/206 path,
    so the file open doesn't re-run the ACL EXISTS predicate and the
    ``attachment_blobs`` SELECT that the probe just ran (#64).

    The ``Path.exists()`` check stays so a blob deleted between probe
    and open surfaces as ``NotFound`` rather than a mid-stream
    ``FileNotFoundError``. Caller closes the returned file. Raises
    ``NotFound`` if the on-disk file is missing.

    Underscore-prefixed and accepts a raw ``path`` rather than ``conn``:
    both make it obvious at every call site that this skips ACL on
    purpose, so it can't be reached for "by accident" the way the prior
    ``open_attachment_bytes(..., prefetched=...)`` kwarg could (#67).
    """
    p = Path(path)
    if not p.exists():
        raise NotFound(f"attachment {sha256_hex} file missing at {path}")
    return p.open("rb")


def open_attachment_bytes(
    conn: psycopg.Connection,
    sha256_hex: str,
    *,
    allowed_account_ids: list[int],
) -> tuple[BinaryIO, str, int]:
    """Open the blob file for streaming. Returns ``(file, mime_type, size)``.

    Caller closes the file. Raises NotFound if the DB row is missing, the
    on-disk file is missing, or the caller's ACL does not include any
    account that references the blob.

    Safe-by-default — always runs the ACL EXISTS predicate. The streaming
    route's 200/206 fast path uses the probe + :func:`_open_blob_file_at`
    pair instead, which is the only place in the codebase that skips the
    second ACL roundtrip (#64, #67).
    """
    mime, size, path = _lookup_blob_row(
        conn, sha256_hex, allowed_account_ids=allowed_account_ids,
    )
    return _open_blob_file_at(path, sha256_hex), mime, size


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
    if not _caller_can_read_blob(
        conn, sha256_hex, allowed_account_ids=allowed_account_ids,
    ):
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
