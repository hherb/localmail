# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Message detail and raw RFC822 access for the API."""
from __future__ import annotations

import logging
from typing import Any

import psycopg

from localmail.api.errors import NotFound, ValidationFailed
from localmail.api.sanitize import sanitize_html
from localmail.header_block import (
    HEADER_BLOCK_READ_BYTES,
    HeaderEntry,
    entries_to_wire,
    group_entries,
    header_block,
    header_mode_error,
    parse_header_block,
)

logger = logging.getLogger("localmail.api.messages")


def get_message(
    conn: psycopg.Connection,
    message_id: int,
    *,
    allowed_account_ids: list[int],
    headers: str = "compact",
    allow_external_images: bool = False,
) -> dict[str, Any]:
    """Return a structured representation of one message.

    Returns `NotFound` if the message does not exist *or* the caller is not
    permitted to read its account — these two cases share the same 404 so
    permission state cannot be enumerated through the API.

    HTML body is server-sanitized; cid: image refs are rewritten to
    /v1/attachments/<sha256> when the corresponding attachment is present.
    """
    mode_problem = header_mode_error(headers)
    if mode_problem is not None:
        raise ValidationFailed(mode_problem)
    if not allowed_account_ids:
        raise NotFound(f"message {message_id} not found")
    with conn.cursor() as cur:
        wants_headers = headers != "compact"
        header_col = (
            ", substring(m.raw_bytes from 1 for %(limit)s) AS header_prefix"
            if wants_headers else ""
        )
        cur.execute(
            f"""
            SELECT m.id, m.account_id, m.subject, m.from_addr, m.from_name,
                   m.to_addrs, m.cc_addrs, m.bcc_addrs, m.body_text, m.body_html,
                   m.attachments, m.date_sent,
                   a.name AS account_name, a.email_address AS account_address
                   {header_col}
              FROM messages m
              JOIN accounts a ON a.id = m.account_id
             WHERE m.id = %(mid)s AND m.account_id = ANY(%(accounts)s)
            """,  # noqa: S608 - header_col is a literal chosen by `wants_headers`
            {
                "mid": message_id,
                "accounts": allowed_account_ids,
                # +1 so a block that fills the ceiling is distinguishable from
                # one that ends exactly at it, without a second query.
                "limit": HEADER_BLOCK_READ_BYTES + 1,
            },
        )
        row = cur.fetchone()
        if row is None:
            raise NotFound(f"message {message_id} not found")
        cur.execute(
            """
            SELECT mb.id, mb.name
              FROM message_labels ml
              JOIN mailboxes mb ON mb.id = ml.mailbox_id
             WHERE ml.message_id = %s
             ORDER BY mb.name
            """,
            (message_id,),
        )
        folder_rows = cur.fetchall()

    (mid, account_id, subject, from_addr, from_name,
     to_addrs, cc_addrs, bcc_addrs, body_text, body_html,
     attachments, date_sent,
     account_name, account_address) = row[:14]

    cid_to_sha = _build_cid_map(attachments or [])
    sanitized_html = (
        sanitize_html(
            body_html or "",
            cid_to_sha=cid_to_sha,
            allow_external_images=allow_external_images,
        )
        if body_html else None
    )
    blob_meta = _load_attachment_meta(conn, attachments or [])

    msg: dict[str, Any] = {
        "id": str(mid),
        "subject": subject,
        "from": _address(from_addr, from_name),
        "to": [_address(a, None) for a in (to_addrs or [])],
        "cc": [_address(a, None) for a in (cc_addrs or [])],
        "bcc": [_address(a, None) for a in (bcc_addrs or [])],
        "date": date_sent.isoformat() if date_sent else None,
        "body_text": body_text,
        "body_html": sanitized_html,
        "attachments": [
            _attachment_entry(a, blob_meta) for a in (attachments or [])
        ],
        "account": {"id": str(account_id), "name": account_name, "address": account_address},
        "folders": [{"id": str(fid), "name": fname} for fid, fname in folder_rows],
    }
    if wants_headers:
        entries = _header_entries(
            conn, message_id,
            allowed_account_ids=allowed_account_ids,
            prefix=bytes(row[14]),
        )
        msg["headers"] = (
            entries_to_wire(entries) if headers == "list" else group_entries(entries)
        )
    return msg


def _header_entries(
    conn: psycopg.Connection,
    message_id: int,
    *,
    allowed_account_ids: list[int],
    prefix: bytes,
) -> list[HeaderEntry]:
    """Occurrences from the prefix, re-reading in full if it was cut short."""
    block = header_block(prefix, truncated=len(prefix) > HEADER_BLOCK_READ_BYTES)
    if block is None:
        # Two distinct causes reach this branch: a genuinely oversized header
        # block, or a message using bare `\r` line endings — `header_block`
        # only recognises `\r\n\r\n`/`\n\n`, so a tiny block with no
        # recognised separator looks identical to a truncated one. State the
        # observation, not a conclusion about the block's size.
        logger.warning(
            "no header/body separator found in the first %d bytes of "
            "message %s; re-reading in full",
            HEADER_BLOCK_READ_BYTES, message_id,
        )
        raw = get_message_raw(
            conn, message_id, allowed_account_ids=allowed_account_ids
        )
        whole = header_block(raw, truncated=False)
        block = b"" if whole is None else whole
    return parse_header_block(block)


def get_message_raw(
    conn: psycopg.Connection,
    message_id: int,
    *,
    allowed_account_ids: list[int],
) -> bytes:
    """Return the raw RFC822 bytes for a message; 404 if outside the caller's ACL."""
    if not allowed_account_ids:
        raise NotFound(f"message {message_id} not found")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT raw_bytes FROM messages "
            "WHERE id = %s AND account_id = ANY(%s)",
            (message_id, allowed_account_ids),
        )
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"message {message_id} not found")
    return bytes(row[0])


def _address(addr: str | None, name: str | None) -> dict[str, str | None]:
    return {"address": addr, "name": name}


def _load_attachment_meta(
    conn: psycopg.Connection, attachments: list[dict[str, Any]]
) -> dict[str, tuple[str | None, int]]:
    """Return ``{sha256_hex: (mime_type, size_bytes)}`` for the message's blobs.

    One batched lookup over ``attachment_blobs`` keyed on the sha256s the
    message references — the stored MIME type and decoded byte length that
    ``get_message`` surfaces as each entry's ``content_type`` / ``size`` (#196).
    A sha with no blob row is simply absent from the map (callers degrade to
    ``None``).
    """
    sha_hexes = {a["sha256"] for a in attachments if a.get("sha256")}
    sha_bytes = []
    for h in sha_hexes:
        try:
            sha_bytes.append(bytes.fromhex(h))
        except ValueError:
            # A malformed sha in the JSONB (corruption, a bad importer) must not
            # 500 the whole message — it just misses the lookup and degrades to
            # null metadata, like an absent blob row.
            continue
    if not sha_bytes:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sha256, mime_type, size_bytes FROM attachment_blobs "
            "WHERE sha256 = ANY(%s)",
            (sha_bytes,),
        )
        return {bytes(sha).hex(): (mime, size) for sha, mime, size in cur.fetchall()}


def _attachment_entry(
    att: dict[str, Any], blob_meta: dict[str, tuple[str | None, int]]
) -> dict[str, Any]:
    sha = att.get("sha256")
    mime: str | None = None
    size: int | None = None
    if sha is not None and sha in blob_meta:
        mime, size = blob_meta[sha]
    return {
        "filename": att.get("filename"),
        "sha256": sha,
        "content_type": mime,
        "size": size,
    }


def _build_cid_map(attachments: list[dict[str, Any]]) -> dict[str, str]:
    """Build a Content-ID to sha256 map for cid: rewriting.

    Reads the ``content_id`` field from each attachment JSONB row and returns
    a ``{cid_token: sha256_hex}`` map the HTML sanitiser uses to rewrite
    ``<img src="cid:…">`` references to ``/v1/attachments/<sha256>``. The
    parser strips angle brackets when populating ``content_id``; the
    ``strip("<>")`` here is defence-in-depth against legacy rows.
    """
    out: dict[str, str] = {}
    for att in attachments:
        cid = att.get("content_id")
        sha = att.get("sha256")
        if cid and sha:
            out[cid.strip("<>")] = sha
    return out
