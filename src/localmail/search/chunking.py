"""Pure-function chunking helpers for the search subsystem.

Splits message bodies into header + body chunks suitable for embedding,
strips email reply chains and signatures so the index doesn't double-count
quoted content. All functions are pure: no IO, no DB, no logging.
"""

from __future__ import annotations

import re

_QUOTE_HEADER_PATTERNS = [
    # English: "On <date>, <name> wrote:"
    re.compile(r"^On .+,\s.+\swrote:\s*$", re.MULTILINE | re.IGNORECASE),
    # German: "Am <date> schrieb <name>:" / "schrieb <name> <addr>:"
    re.compile(r"^Am .+\sschrieb\s.+:\s*$", re.MULTILINE | re.IGNORECASE),
    # Spanish: "El <date>, <name> escribió:"
    re.compile(r"^El .+,\s.+\sescribi[oó]:\s*$", re.MULTILINE | re.IGNORECASE),
    # Generic Outlook divider
    re.compile(r"^-----\s*Original Message\s*-----\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^From:.+\nSent:.+\nTo:.+", re.MULTILINE),
]

_ARROW_QUOTE_LINE = re.compile(r"^\s*>.*$", re.MULTILINE)
_SIGNATURE_DELIM = re.compile(r"^-- ?$", re.MULTILINE)
_WS_INLINE = re.compile(r"[ \t]+")
_WS_BLANKLINES = re.compile(r"\n{3,}")


def strip_quoted_replies(body: str) -> str:
    """Remove quoted reply chains from an email body.

    Cuts at the first quote-header marker (e.g. 'On ... wrote:', German
    equivalent, Spanish equivalent, '----- Original Message -----'),
    then drops any remaining '>'-prefixed quote lines from what's left.
    """
    earliest: int | None = None
    for pat in _QUOTE_HEADER_PATTERNS:
        m = pat.search(body)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    truncated = body[:earliest] if earliest is not None else body
    return _ARROW_QUOTE_LINE.sub("", truncated)


def strip_signature(body: str) -> str:
    """Remove an email signature delimited by a '-- ' line (RFC 3676).

    The delimiter is a line containing exactly '-- ' (dash-dash-space) or
    '--'. Everything from the first such delimiter onward is removed.
    """
    m = _SIGNATURE_DELIM.search(body)
    return body[: m.start()] if m else body


def normalize_whitespace(text: str) -> str:
    """Collapse runs of inline whitespace and blank-line runs.

    - Tabs and multi-space runs collapse to single space.
    - Three-or-more consecutive newlines collapse to two (one blank line).
    - Leading/trailing whitespace on each line, and on the whole text, is trimmed.
    """
    lines = [_WS_INLINE.sub(" ", line).strip() for line in text.splitlines()]
    joined = "\n".join(lines)
    return _WS_BLANKLINES.sub("\n\n", joined).strip()


from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")
_HEADER_BODY_INTRO_TOKENS = 200


@dataclass(frozen=True)
class ChunkSpec:
    """One chunk awaiting INSERT into message_chunks or attachment_chunks."""
    kind: Literal["header", "body", "attachment"]
    chunk_idx: int
    text: str
    token_count: int


@dataclass(frozen=True)
class MessageRow:
    """Minimal shape chunk_message needs from a messages row.

    Hydrated from the columns embed_worker selects; keeps chunking
    decoupled from the DB read path.
    """
    id: int
    subject: str | None
    from_addr: str | None
    from_name: str | None
    to_addrs: list[str] | None
    date_sent: datetime | None
    body_text: str | None


def split_by_tokens(text: str, size: int, overlap: int) -> list[str]:
    """Split text into token-windowed chunks of `size` with `overlap` tokens shared.

    Tokenization uses cl100k_base as a neutral approximation across
    embedding models — the resulting chunk sizes are soft targets, not
    hard guarantees on the model's true token budget.
    """
    if size <= 0:
        raise ValueError("size must be > 0")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be in [0, size)")
    tokens = _ENC.encode(text)
    if len(tokens) <= size:
        return [text] if text else []
    out: list[str] = []
    step = size - overlap
    for start in range(0, len(tokens), step):
        window = tokens[start : start + size]
        if not window:
            break
        out.append(_ENC.decode(window))
        if start + size >= len(tokens):
            break
    return out


def _header_text(msg: MessageRow, body_for_intro: str) -> str:
    """Build the structured header-chunk text for one message."""
    parts: list[str] = []
    if msg.subject:
        parts.append(f"Subject: {msg.subject}")
    if msg.from_name or msg.from_addr:
        who = f"{msg.from_name} <{msg.from_addr}>" if msg.from_name else (msg.from_addr or "")
        parts.append(f"From: {who.strip()}")
    if msg.to_addrs:
        parts.append(f"To: {', '.join(msg.to_addrs)}")
    if msg.date_sent:
        parts.append(f"Date: {msg.date_sent.isoformat()}")
    intro_tokens = _ENC.encode(body_for_intro)[:_HEADER_BODY_INTRO_TOKENS]
    if intro_tokens:
        parts.append(_ENC.decode(intro_tokens))
    return " | ".join(parts)


def chunk_attachment_text(
    sha256: bytes,
    text: str,
    cfg,
) -> list[ChunkSpec]:
    """Token-aware chunking for extracted attachment text. Pure function.

    Args:
        sha256: The blob's sha256 (unused for chunking but kept in the
            signature so callers can pass it explicitly — useful for
            future extensions that key chunks back to the source blob).
        text: The extracted plain-text from one attachment_text row.
        cfg: SearchConfig — supplies chunk_size_tokens,
            chunk_overlap_tokens, and extractor_max_extracted_chars.

    Returns:
        A list of ChunkSpec records with kind='attachment' and ordered
        chunk_idx starting at 0. Returns [] for empty input (the
        embed_worker uses this to silently skip sentinel
        attachment_text rows with extracted_text='').

    Behavior:
        - Normalises whitespace before chunking (collapses tabs, runs
          of spaces, and runs of 3+ newlines).
        - If the input exceeds cfg.extractor_max_extracted_chars, the
          tail is truncated and the last chunk gets a '[truncated]'
          marker appended (Rule 6 — truncation is user-approved via
          SearchConfig).
        - Splits via the existing token-budget chunker using
          cfg.chunk_size_tokens and cfg.chunk_overlap_tokens.
    """
    text = normalize_whitespace(text or "")
    if not text:
        return []

    truncated = False
    if len(text) > cfg.extractor_max_extracted_chars:
        text = text[: cfg.extractor_max_extracted_chars]
        truncated = True

    pieces = split_by_tokens(
        text,
        size=cfg.chunk_size_tokens,
        overlap=cfg.chunk_overlap_tokens,
    )
    if truncated and pieces:
        pieces[-1] = pieces[-1] + "\n[truncated]"

    chunks: list[ChunkSpec] = []
    for idx, piece in enumerate(pieces):
        chunks.append(
            ChunkSpec(
                kind="attachment",
                chunk_idx=idx,
                text=piece,
                token_count=len(_ENC.encode(piece)),
            )
        )
    return chunks


def chunk_message(msg: MessageRow, cfg) -> list[ChunkSpec]:
    """Produce header + body chunks for a message.

    - Header chunk (always exactly one): structured metadata + first ~200 tokens of body.
    - Body chunks: rest of body, split at cfg.chunk_size_tokens with cfg.chunk_overlap_tokens.
    - Quoted reply chains and signatures stripped per cfg.chunk_strip_*.
    - If body is None/empty or shorter than ~200 tokens, only the header chunk is emitted.
    """
    raw = msg.body_text or ""
    if cfg.chunk_strip_quoted_replies:
        raw = strip_quoted_replies(raw)
    if cfg.chunk_strip_signatures:
        raw = strip_signature(raw)
    body = normalize_whitespace(raw)

    header_text = _header_text(msg, body)
    chunks: list[ChunkSpec] = [
        ChunkSpec(
            kind="header",
            chunk_idx=0,
            text=header_text,
            token_count=len(_ENC.encode(header_text)),
        )
    ]
    body_tokens = _ENC.encode(body) if body else []
    if len(body_tokens) <= _HEADER_BODY_INTRO_TOKENS:
        return chunks

    remainder = _ENC.decode(body_tokens[_HEADER_BODY_INTRO_TOKENS:])
    for idx, piece in enumerate(
        split_by_tokens(remainder, cfg.chunk_size_tokens, cfg.chunk_overlap_tokens)
    ):
        chunks.append(
            ChunkSpec(
                kind="body",
                chunk_idx=idx,
                text=piece,
                token_count=len(_ENC.encode(piece)),
            )
        )
    return chunks
