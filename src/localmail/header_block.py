# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""One message's header block: its occurrences, in wire order (pure, no IO).

`api/messages.py` serves these and `parser.py` stores their grouping, so the
rule lives beside neither. `full` is `group_entries` of the same sequence
`list` emits, which is what keeps the two modes from disagreeing.
"""
from __future__ import annotations

import email
import email.policy
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Literal, get_args

from localmail.pgtext import strip_nuls

HeaderMode = Literal["compact", "full", "list"]

# Derived, never restated: CI runs no mypy step, so a hand-typed tuple could
# silently drift wider than the Literal MCP's `Field` validates against —
# HTTP would then accept a mode MCP refuses, and `api/messages.py`'s
# `else` branch would serve the extra mode as `full`. Deriving from the
# Literal itself, rather than checking one against the other, makes that
# drift impossible by construction instead of merely caught.
HEADER_MODES: tuple[HeaderMode, ...] = get_args(HeaderMode)

# The block is read as a bounded prefix of `raw_bytes`, which carries the
# attachments too: p50 44 KB, p99 2.6 MB, max 35 MB on the live archive against
# a p95 header block of 8.7 KB. Measured for #379: 0 of 129,590 messages have a
# block that does not end within this ceiling.
HEADER_BLOCK_READ_BYTES = 64 * 1024

# Each separator is a doubled line terminator: the first occurrence ends the last
# header line, the second occurrence is the blank line. We return up to and
# including the first occurrence, omitting the blank line itself.
_SEPARATORS = (
    (b"\r\n\r\n", 2),  # separator pattern, length of line ending to include
    (b"\n\n", 1),
)


@dataclass(frozen=True)
class HeaderEntry:
    """One occurrence: the name as spelled on the wire, and its value."""

    name: str
    value: str


def header_mode_error(value: str) -> str | None:
    """A message naming the accepted modes, or None when `value` is one."""
    if value in HEADER_MODES:
        return None
    accepted = ", ".join(repr(m) for m in HEADER_MODES)
    return f"headers must be one of {accepted}; got {value!r}"


def header_block(data: bytes, *, truncated: bool) -> bytes | None:
    """The bytes before the first blank line, or None if it has none.

    `truncated` says whether `data` was cut short by a read ceiling. Without a
    separator the answer differs: a complete message simply has no body (RFC
    5322 permits it), while a truncated one may have the rest of its headers
    past the cut — reporting None is what stops a short list passing for a
    complete one.
    """
    separator_matches = []
    for sep_pattern, ending_len in _SEPARATORS:
        idx = data.find(sep_pattern)
        if idx >= 0:
            separator_matches.append((idx, ending_len))
    if separator_matches:
        min_idx, ending_len = min(separator_matches)
        return data[: min_idx + ending_len]
    return None if truncated else data


def entries_from_message(msg: EmailMessage) -> list[HeaderEntry]:
    """Every occurrence, degrading only the ones the parser chokes on.

    `raw_items()` is the wire sequence unparsed, so each occurrence is parsed
    individually and a failing one falls back to its raw text rather than
    costing the other headers (#314).
    """
    out: list[HeaderEntry] = []
    for name, raw_value in msg.raw_items():
        try:
            value = str(msg.policy.header_fetch_parse(name, raw_value))
        except Exception:
            value = raw_value
        out.append(HeaderEntry(name=name, value=strip_nuls(value)))
    return out


def parse_header_block(block: bytes) -> list[HeaderEntry]:
    """Occurrences in `block`, under the policy `parse_message` reads with."""
    msg = email.message_from_bytes(
        block, _class=EmailMessage, policy=email.policy.default
    )
    return entries_from_message(msg)


def group_entries(entries: Iterable[HeaderEntry]) -> dict[str, list[str]]:
    """The `headers=full` shape: one key per wire spelling, values in order."""
    out: dict[str, list[str]] = {}
    for entry in entries:
        out.setdefault(entry.name, []).append(entry.value)
    return out


def entries_to_wire(entries: Iterable[HeaderEntry]) -> list[dict[str, str]]:
    """The `headers=list` payload."""
    return [{"name": e.name, "value": e.value} for e in entries]
