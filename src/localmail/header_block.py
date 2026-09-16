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
from typing import Literal

from localmail.pgtext import strip_nuls

HeaderMode = Literal["compact", "full", "list"]
HEADER_MODES: tuple[HeaderMode, ...] = ("compact", "full", "list")

# The block is read as a bounded prefix of `raw_bytes`, which carries the
# attachments too: p50 44 KB, p99 2.6 MB, max 35 MB on the live archive against
# a p95 header block of 8.7 KB. Measured for #379: 0 of 129,590 messages have a
# block that does not end within this ceiling.
HEADER_BLOCK_READ_BYTES = 64 * 1024

_SEPARATORS = (b"\r\n\r\n", b"\n\n")


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
    ends = []
    for sep in _SEPARATORS:
        idx = data.find(sep)
        if idx >= 0:
            ends.append((idx, len(sep)))
    if ends:
        min_idx, sep_len = min(ends)
        return data[: min_idx + sep_len // 2]
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
