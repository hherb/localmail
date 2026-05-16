"""RFC822 -> structured ParsedMessage (no side effects, no DB, no IO)."""

from __future__ import annotations

import email
import email.policy
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import getaddresses


@dataclass
class Attachment:
    filename: str
    mime_type: str
    payload: bytes


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


def _no_nul(s: str | None) -> str | None:
    """Strip NUL bytes — PostgreSQL TEXT rejects them. Real-world mail
    occasionally contains \\x00 in subject/body when a sender mangles encodings
    or attaches binary garbage to a text part."""
    if s is None or "\x00" not in s:
        return s
    return s.replace("\x00", "")


def _no_nul_list(xs: list[str]) -> list[str]:
    return [x.replace("\x00", "") if "\x00" in x else x for x in xs]


def _decode_part_text(part: EmailMessage) -> str | None:
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        # Unknown charset or undecodable bytes: fall back to raw bytes decoded loosely.
        payload = part.get_payload(decode=True) or b""
        try:
            return payload.decode("utf-8", errors="replace")
        except Exception:
            return None
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


def _attachments(msg: EmailMessage) -> list[Attachment]:
    out: list[Attachment] = []
    for part in msg.iter_attachments():
        filename = part.get_filename() or "attachment"
        payload = part.get_payload(decode=True) or b""
        out.append(
            Attachment(
                filename=filename,
                mime_type=part.get_content_type(),
                payload=payload,
            )
        )
    return out


def parse_message(raw: bytes) -> ParsedMessage:
    msg = email.message_from_bytes(raw, _class=EmailMessage, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)

    from_addr, from_name = _from_pair(msg)
    text, html = _bodies(msg)

    message_id = msg.get("Message-Id")
    message_id = str(message_id) if message_id else None

    in_reply_to = msg.get("In-Reply-To")
    in_reply_to = str(in_reply_to) if in_reply_to else None

    subject = msg.get("Subject")
    subject = str(subject) if subject is not None else None

    headers = {k: _no_nul_list(vs) for k, vs in _headers_dict(msg).items()}
    attachments = _attachments(msg)
    # Normalize "" -> None so empty subjects/bodies land as SQL NULL rather
    # than as an empty string the schema doesn't require.
    subject_clean = _no_nul(subject) or None
    text_clean = _no_nul(text) or None
    html_clean = _no_nul(html) or None

    # Attachment-only messages (no subject, no text body) would otherwise be
    # invisible to FTS and to human browsers. Synthesize a placeholder so the
    # message has something to surface on — the original attachments are still
    # available verbatim via messages.attachments JSONB + the blobs tree.
    if attachments:
        if not subject_clean:
            subject_clean = "{attachments only}"
        if not text_clean:
            names = [(_no_nul(a.filename) or "attachment") for a in attachments]
            text_clean = "{attachments: " + ", ".join(names) + "}"

    return ParsedMessage(
        message_id=_no_nul(message_id),
        raw_sha256=hashlib.sha256(raw).digest(),
        in_reply_to=_no_nul(in_reply_to),
        refs=_no_nul_list(_refs_list(msg)),
        subject=subject_clean,
        from_addr=_no_nul(from_addr),
        from_name=_no_nul(from_name),
        to_addrs=_no_nul_list(_address_list(msg, "To")),
        cc_addrs=_no_nul_list(_address_list(msg, "Cc")),
        bcc_addrs=_no_nul_list(_address_list(msg, "Bcc")),
        date_sent=_date_sent(msg),
        headers=headers,
        body_text=text_clean,
        body_html=html_clean,
        raw_bytes=raw,
        size_bytes=len(raw),
        attachments=attachments,
    )
