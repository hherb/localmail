# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Content-addressable attachment storage.

Each attachment payload is stored exactly once on disk, at
`<root>/blobs/<aa>/<bb>/<sha256_hex>`, and indexed by a row in the
`attachment_blobs` table keyed on the 32-byte sha256. A message's
`messages.attachments` JSONB column records, per attachment in that message:

    {"filename": "<original filename from this email>",
     "sha256": "<hex>",
     "content_id": "<inline-cid-without-brackets>"}  # omitted when None

`content_id` is the message-local Content-Id header value with the angle
brackets stripped (e.g. "image1@example"). Downstream HTML rendering uses it
to rewrite `<img src="cid:…">` references to actual blob URLs so inline
images render. Non-inline attachments omit the key.

The original `filename` is preserved per-message so files can be restored
with the names they had when received. The bytes are deduplicated across all
messages and accounts.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

import psycopg

from .parser import ParsedMessage

_UNSAFE = re.compile(r"[/\\\x00-\x1f\x7f]")


def sanitize_filename(name: str) -> str:
    """Render a filename safe for an arbitrary FS without leaking path separators.

    Used only for the *recorded* filename in the JSONB column — the on-disk
    name is the sha256 hex and is never derived from this.
    """
    if not name or not name.strip():
        return "attachment"
    cleaned = _UNSAFE.sub("_", name)
    cleaned = cleaned.lstrip("._")
    if not cleaned:
        return "attachment"
    return cleaned


def blob_path(root: Path, sha256_hex: str) -> Path:
    """Return the on-disk path for a blob, two-level fan-out by hex prefix."""
    return Path(root) / "blobs" / sha256_hex[:2] / sha256_hex[2:4] / sha256_hex


def write_attachments(
    conn: psycopg.Connection,
    parsed: ParsedMessage,
    *,
    root: Path,
) -> list[dict]:
    """Write any not-yet-stored attachment payloads to the blob tree and upsert
    `attachment_blobs` rows. Return the per-message entries ready for
    `messages.attachments` JSONB. Each entry has `filename` and `sha256`; an
    inline attachment also carries `content_id` (sans angle brackets) so
    HTML body `cid:` references can be rewritten on render.

    The caller owns the surrounding transaction; this function does not commit.
    """
    if not parsed.attachments:
        return []

    rows: list[dict] = []
    with conn.cursor() as cur:
        for att in parsed.attachments:
            digest = hashlib.sha256(att.payload).digest()
            sha_hex = digest.hex()
            path = blob_path(root, sha_hex)

            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                # Unique temp name per writer: the daemon runs two threads per
                # account (IDLE + poll) and Gmail delivers the same message to
                # INBOX and other labels at once, so two threads can write the
                # same blob concurrently. A shared `<sha>.tmp` let one writer
                # truncate the temp the other was about to atomically `replace`
                # into place, installing a corrupted (short/zero) blob at the
                # canonical path. A uuid suffix keeps each writer's temp private;
                # both hold identical bytes, so whichever `replace` wins is
                # correct. content-addressed, so a redundant write is harmless.
                tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
                try:
                    tmp.write_bytes(att.payload)
                    tmp.replace(path)
                finally:
                    tmp.unlink(missing_ok=True)

            cur.execute(
                """
                INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (sha256) DO NOTHING
                """,
                (digest, str(path.resolve()), att.mime_type, len(att.payload)),
            )

            row: dict = {"filename": sanitize_filename(att.filename), "sha256": sha_hex}
            if att.content_id:
                row["content_id"] = att.content_id
            rows.append(row)

    return rows
