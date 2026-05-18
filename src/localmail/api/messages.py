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
    full_headers: bool = False,
) -> dict[str, Any]:
    """Return a structured representation of one message.

    HTML body is server-sanitized; cid: image refs are rewritten to
    /v1/attachments/<sha256> when the corresponding attachment is present.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.account_id, m.subject, m.from_addr, m.from_name,
                   m.to_addrs, m.cc_addrs, m.bcc_addrs, m.body_text, m.body_html,
                   m.attachments, m.headers, m.date_sent,
                   a.name AS account_name, a.email_address AS account_address
              FROM messages m
              JOIN accounts a ON a.id = m.account_id
             WHERE m.id = %s
            """,
            (message_id,),
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

    cid_to_sha = _build_cid_map(attachments or [], headers or {})
    sanitized_html = sanitize_html(body_html or "", cid_to_sha=cid_to_sha) if body_html else None

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
            {"filename": a.get("filename"), "sha256": a.get("sha256")}
            for a in (attachments or [])
        ],
        "account": {"id": str(account_id), "name": account_name, "address": account_address},
        "folders": [{"id": str(fid), "name": fname} for fid, fname in folder_rows],
    }
    if full_headers:
        msg["headers"] = headers or {}
    return msg


def get_message_raw(conn: psycopg.Connection, message_id: int) -> bytes:
    """Return the raw RFC822 bytes for a message."""
    with conn.cursor() as cur:
        cur.execute("SELECT raw_bytes FROM messages WHERE id = %s", (message_id,))
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"message {message_id} not found")
    return bytes(row[0])


def _address(addr: str | None, name: str | None) -> dict[str, str | None]:
    return {"address": addr, "name": name}


def _build_cid_map(attachments: list[dict[str, Any]], headers: dict[str, Any]) -> dict[str, str]:
    """Build a Content-ID to sha256 map for cid: rewriting.

    Reads the ``content_id`` field from each attachment JSONB row. The current
    parser/write_attachments path does not yet persist ``content_id`` — tracked
    in a separate issue — so this map is empty in practice today and inline
    images get ``src=""`` after sanitisation. The wiring is correct so the
    rewrite begins working as soon as the parser side lands.
    """
    out: dict[str, str] = {}
    for att in attachments:
        cid = att.get("content_id")
        sha = att.get("sha256")
        if cid and sha:
            out[cid.strip("<>")] = sha
    return out
