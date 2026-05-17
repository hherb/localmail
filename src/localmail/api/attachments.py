"""Attachment metadata, streaming, and extracted-text accessors."""
from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

import psycopg

from localmail.api.errors import NotFound


def get_attachment_metadata(conn: psycopg.Connection, sha256_hex: str) -> dict[str, object]:
    """Return {sha256, mime_type, size_bytes} for a blob. Raises NotFound."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mime_type, size_bytes FROM attachment_blobs WHERE sha256 = %s",
            (bytes.fromhex(sha256_hex),),
        )
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"attachment {sha256_hex} not found")
    return {
        "sha256": sha256_hex,
        "mime_type": row[0],
        "size_bytes": int(row[1]),
    }


def open_attachment_bytes(
    conn: psycopg.Connection, sha256_hex: str
) -> tuple[BinaryIO, str, int]:
    """Open the blob file for streaming. Returns (file, mime_type, size).

    Caller closes the file. Raises NotFound if the DB row or on-disk file is missing.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mime_type, size_bytes, path FROM attachment_blobs WHERE sha256 = %s",
            (bytes.fromhex(sha256_hex),),
        )
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"attachment {sha256_hex} not found")
    mime, size, path = row
    p = Path(path)
    if not p.exists():
        raise NotFound(f"attachment {sha256_hex} file missing at {path}")
    return p.open("rb"), mime, int(size)


def get_attachment_text(conn: psycopg.Connection, sha256_hex: str) -> str:
    """Return extracted text for a blob. Raises NotFound if not yet extracted."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT extracted_text FROM attachment_text WHERE sha256 = %s",
            (bytes.fromhex(sha256_hex),),
        )
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"no extracted text for attachment {sha256_hex}")
    return row[0]
