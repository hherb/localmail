# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Paging a stored text by characters.

Pure: no IO. Both text routes — ``/v1/attachments/{sha256}/text`` and
``/v1/messages/{id}/attachments/{index}/text`` — page through this module, so
the validation, the conversion to Postgres' 1-based ``substring()`` positions,
and the ``next_offset`` arithmetic are decided once.

Characters are code points: what Postgres' ``length()``/``substring()`` count
on a UTF-8 database and what Python's ``len(str)`` counts. They are not
JavaScript's UTF-16 units, which is why ``next_offset`` is computed here rather
than left to the client.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Postgres caps a single field value at 1 GB, and a character is at least one
#: byte, so no TEXT value is longer than this. `substring()` takes int4
#: positions — a larger one is `function substring(…, bigint, …) does not
#: exist`, a 500 — so offsets and limits are clamped here, which cannot change
#: an answer: every offset at or past this is past the end of every text.
MAX_TEXT_CHARS = 2**30


def text_window_error(offset: int, limit: int | None) -> str | None:
    """Why ``(offset, limit)`` is not a window, or ``None`` when it is.

    A zero ``limit`` is refused, not served: it would answer
    ``next_offset == offset`` for any text not yet exhausted, and a client
    looping on ``next_offset`` would never advance.
    """
    if offset < 0:
        return f"offset must be >= 0, got {offset}"
    if limit is not None and limit < 1:
        return f"limit must be >= 1, got {limit}"
    return None


@dataclass(frozen=True)
class TextPage:
    """One window of a text, with what a client needs to fetch the next."""

    text: str
    offset: int
    limit: int | None
    total: int
    next_offset: int | None

    def to_wire(self) -> dict[str, object]:
        return {
            "text": self.text,
            "offset": self.offset,
            "limit": self.limit,
            "total": self.total,
            "next_offset": self.next_offset,
        }


@dataclass(frozen=True)
class TextWindow:
    """A validated ``(offset, limit)``; ``limit=None`` means to the end.

    The 1-based conversion lives on the object that validated ``offset``,
    because ``offset + 1`` is only a correct ``substring()`` position when
    ``offset >= 0`` holds.
    """

    offset: int = 0
    limit: int | None = None

    def __post_init__(self) -> None:
        # The by-construction backstop: the HTTP boundary words refusals
        # through `text_window_error` first, so no wire input reaches this.
        problem = text_window_error(self.offset, self.limit)
        if problem is not None:
            raise ValueError(problem)

    @property
    def sql_from(self) -> int:
        return min(self.offset, MAX_TEXT_CHARS) + 1

    @property
    def sql_for(self) -> int | None:
        return None if self.limit is None else min(self.limit, MAX_TEXT_CHARS)

    def page(self, text: str, total: int) -> TextPage:
        end = self.offset + len(text)
        return TextPage(
            text=text, offset=self.offset, limit=self.limit, total=total,
            next_offset=end if end < total else None,
        )
