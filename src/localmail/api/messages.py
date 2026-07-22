# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Message detail and raw RFC822 access for the API."""
from __future__ import annotations

from typing import Any

import psycopg

from localmail.api.errors import NotFound
from localmail.api.sanitize import sanitize_html


def get_message(
    conn: psycopg.Connection,
    message_id: int,
    *,
    allowed_account_ids: list[int],
    full_headers: bool = False,
) -> dict[str, Any]:
    """Return a structured representation of one message.

    Returns `NotFound` if the message does not exist *or* the caller is not
    permitted to read its account — these two cases share the same 404 so
    permission state cannot be enumerated through the API.

    HTML body is server-sanitized; cid: image refs are rewritten to
    /v1/attachments/<sha256> when the corresponding attachment is present.
    """
    if not allowed_account_ids:
        raise NotFound(f"message {message_id} not found")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.account_id, m.subject, m.from_addr, m.from_name,
                   m.to_addrs, m.cc_addrs, m.bcc_addrs, m.body_text, m.body_html,
                   m.attachments, m.headers, m.date_sent,
                   a.name AS account_name, a.email_address AS account_address
              FROM messages m
              JOIN accounts a ON a.id = m.account_id
             WHERE m.id = %s AND m.account_id = ANY(%s)
            """,
            (message_id, allowed_account_ids),
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
     attachments, headers, date_sent,
     account_name, account_address) = row

    cid_to_sha = _build_cid_map(attachments or [])
    sanitized_html = sanitize_html(body_html or "", cid_to_sha=cid_to_sha) if body_html else None
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
    if full_headers:
        msg["headers"] = headers or {}
    return msg


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
    if not sha_hexes:
        return {}
    sha_bytes = [bytes.fromhex(h) for h in sha_hexes]
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
