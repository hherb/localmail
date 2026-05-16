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
