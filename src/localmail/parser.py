# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""RFC822 -> structured ParsedMessage (no side effects, no DB, no IO)."""

from __future__ import annotations

import email
import email.policy
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage, MIMEPart
from email.utils import getaddresses
from typing import Any

from localmail.pgtext import strip_nuls, strip_nuls_all


@dataclass
class Attachment:
    filename: str
    mime_type: str
    payload: bytes
    # Content-ID header value with the angle brackets stripped (e.g.
    # "image1@example"), or None for non-inline attachments. Used downstream
    # to rewrite `<img src="cid:…">` in HTML bodies to the actual blob URL.
    content_id: str | None = None


@dataclass
class ParsedMessage:
    message_id: str | None
    raw_sha256: bytes
    in_reply_to: str | None
    refs: list[str]
    subject: str | None
    from_addr: str | None
    from_name: str | None
    to_addrs: list[str]
    cc_addrs: list[str]
    bcc_addrs: list[str]
    date_sent: datetime | None
    headers: dict[str, list[str]]
    body_text: str | None
    body_html: str | None
    raw_bytes: bytes
    size_bytes: int
    attachments: list[Attachment] = field(default_factory=list)


def _address_list(msg: EmailMessage, header: str) -> list[str]:
    raw = msg.get_all(header, [])
    if not raw:
        return []
    return [addr for _, addr in getaddresses(raw) if addr]


def _from_pair(msg: EmailMessage) -> tuple[str | None, str | None]:
    raw = msg.get_all("From", [])
    if not raw:
        return None, None
    pairs = getaddresses(raw)
    if not pairs:
        return None, None
    name, addr = pairs[0]
    return (addr or None), (name or None)


def _refs_list(msg: EmailMessage) -> list[str]:
    raw = msg.get("References")
    if not raw:
        return []
    return [tok for tok in str(raw).split() if tok]


def _date_sent(msg: EmailMessage) -> datetime | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    return dt


def _headers_dict(msg: EmailMessage) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, value in msg.items():
        out.setdefault(name, []).append(str(value))
    return out


def _decoded_payload(part: MIMEPart[Any, Any]) -> bytes:
    """Decoded payload bytes for a leaf part, or ``b""`` when absent.

    ``Message.get_payload(decode=True)`` is typed loosely (``bytes | Any``)
    even though a leaf part yields ``bytes | None`` at runtime; narrow it here
    so callers receive a concrete ``bytes`` instead of an unchecked union.
    """
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def _decode_part_text(part: MIMEPart[Any, Any]) -> str | None:
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        # Unknown charset or undecodable bytes: fall back to raw bytes decoded
        # loosely. errors="replace" guarantees this never raises.
        return _decoded_payload(part).decode("utf-8", errors="replace")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content


def _bodies(msg: EmailMessage) -> tuple[str | None, str | None]:
    text: str | None = None
    html: str | None = None

    text_part = msg.get_body(preferencelist=("plain",))
    if text_part is not None:
        text = _decode_part_text(text_part)

    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None and html_part.get_content_type() == "text/html":
        html = _decode_part_text(html_part)

    return text, html


def _content_id(part: EmailMessage) -> str | None:
    raw = part.get("Content-Id")
    if not raw:
        return None
    cid = str(raw).strip()
    if cid.startswith("<") and cid.endswith(">"):
        cid = cid[1:-1]
    return strip_nuls(cid) or None


def _attachments(msg: EmailMessage) -> list[Attachment]:
    out: list[Attachment] = []
    for part in msg.iter_attachments():
        filename = part.get_filename() or "attachment"
        payload = _decoded_payload(part)
        out.append(
            Attachment(
                filename=filename,
                mime_type=part.get_content_type(),
                payload=payload,
                content_id=_content_id(part),
            )
        )
    return out


def normalize_message_id(value: str | None) -> str | None:
    """Collapse a degenerate `Message-Id` to None so raw-SHA dedup engages (#222B).

    Dedup falls back to `messages.raw_sha256` only when `message_id IS NULL`. A
    header that is present but blank -- `Message-Id:` with nothing but
    whitespace, or an empty angle-addr `<>` from a broken MTA -- is non-None and
    non-unique, so two distinct messages carrying it would collapse onto one row
    and the second message's body and attachments would be discarded.

    Surrounding whitespace is stripped so `<a@b>` and ` <a@b> ` are one message;
    `email.policy.default` already unfolds and strips well-formed headers, so
    this only bites on the degenerate ones.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    # An empty angle-addr carries no identity, only punctuation.
    if stripped.startswith("<") and stripped.endswith(">") and not stripped[1:-1].strip():
        return None
    return stripped


def parse_message(raw: bytes) -> ParsedMessage:
    msg = email.message_from_bytes(raw, _class=EmailMessage, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)

    from_addr, from_name = _from_pair(msg)
    text, html = _bodies(msg)

    raw_message_id = msg.get("Message-Id")
    message_id = normalize_message_id(str(raw_message_id) if raw_message_id else None)

    in_reply_to = msg.get("In-Reply-To")
    in_reply_to = str(in_reply_to) if in_reply_to else None

    subject = msg.get("Subject")
    subject = str(subject) if subject is not None else None

    headers = {k: strip_nuls_all(vs) for k, vs in _headers_dict(msg).items()}
    attachments = _attachments(msg)
    # Normalize "" -> None so empty subjects/bodies land as SQL NULL rather
    # than as an empty string the schema doesn't require.
    subject_clean = strip_nuls(subject) or None
    text_clean = strip_nuls(text) or None
    html_clean = strip_nuls(html) or None

    # Attachment-only messages (no subject, no text body) would otherwise be
    # invisible to FTS and to human browsers. Synthesize a placeholder so the
    # message has something to surface on — the original attachments are still
    # available verbatim via messages.attachments JSONB + the blobs tree.
    if attachments:
        if not subject_clean:
            subject_clean = "{attachments only}"
        if not text_clean:
            names = [(strip_nuls(a.filename) or "attachment") for a in attachments]
            text_clean = "{attachments: " + ", ".join(names) + "}"

    return ParsedMessage(
        message_id=strip_nuls(message_id),
        raw_sha256=hashlib.sha256(raw).digest(),
        in_reply_to=strip_nuls(in_reply_to),
        refs=strip_nuls_all(_refs_list(msg)),
        subject=subject_clean,
        from_addr=strip_nuls(from_addr),
        from_name=strip_nuls(from_name),
        to_addrs=strip_nuls_all(_address_list(msg, "To")),
        cc_addrs=strip_nuls_all(_address_list(msg, "Cc")),
        bcc_addrs=strip_nuls_all(_address_list(msg, "Bcc")),
        date_sent=_date_sent(msg),
        headers=headers,
        body_text=text_clean,
        body_html=html_clean,
        raw_bytes=raw,
        size_bytes=len(raw),
        attachments=attachments,
    )
