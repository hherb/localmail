# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Opaque cursor codec for POST /v1/search, and the mode a cursor continues.

Two cursor kinds share the wire field:

  * Pool cursor (hybrid search) — ``"{search_token}:{page}"``.
  * Keyset cursor (lexical-date search) — ``"K|<base64>"`` where the
    payload is the same encoding ``api.browse_cursor`` uses.

The route layer selects by leading prefix. Clients MUST treat the cursor
as opaque — the prefix is an internal discriminator, not API.

Minting, matching, and *interpreting* live together here because writing
them apart is what produced the defect this module's ``resolve_cursor_mode``
now rules out (#308): the request's ``sort`` was resolved to its default
before the cursor was read, and a cursor the resulting mode could not read
was dropped in silence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from localmail.api.browse_cursor import (
    BrowseCursor, decode_browse_cursor, encode_browse_cursor,
)
from localmail.api.errors import ValidationFailed
from localmail.search.searcher import KeysetCursor, SortMode

_KEYSET_PREFIX = "K|"

#: The sort a request gets when it states none and has no cursor to inherit one from.
DEFAULT_SORT: SortMode = "rank"

#: The only sort a keyset cursor can continue — the lexical-date branch is the
#: sole minter and the sole reader of that cursor kind.
KEYSET_SORT: SortMode = "date"

CursorMode = Literal["fresh", "pool", "keyset"]


@dataclass(frozen=True)
class SearchCursor:
    token: str
    page: int


def encode_search_cursor(cursor: SearchCursor) -> str:
    return f"{cursor.token}:{cursor.page}"


def decode_search_cursor(raw: str) -> SearchCursor:
    if ":" not in raw:
        raise ValidationFailed(f"cursor: missing ':' separator in {raw!r}")
    token, _, page_str = raw.rpartition(":")
    if not token:
        raise ValidationFailed("cursor: empty token")
    if not page_str.isascii() or not page_str.isdigit():
        raise ValidationFailed(f"cursor: page must be a positive integer, got {page_str!r}")
    page = int(page_str)
    if page < 1:
        raise ValidationFailed(f"cursor: page must be >= 1, got {page}")
    return SearchCursor(token=token, page=page)


def is_keyset_cursor(raw: str) -> bool:
    return raw.startswith(_KEYSET_PREFIX)


def encode_keyset_cursor(ks: KeysetCursor) -> str:
    payload = encode_browse_cursor(BrowseCursor(ts=ks.ts, id=ks.id))
    return f"{_KEYSET_PREFIX}{payload}"


def decode_keyset_cursor(raw: str) -> KeysetCursor:
    if not is_keyset_cursor(raw):
        raise ValidationFailed(f"cursor: not a keyset cursor: {raw!r}")
    bc = decode_browse_cursor(raw[len(_KEYSET_PREFIX):])
    return KeysetCursor(ts=bc.ts, id=bc.id)


def resolve_cursor_mode(
    *, cursor: str | None, requested_sort: SortMode | None, free_text: str,
) -> CursorMode:
    """Decide which retrieval mode a request continues — cursor first.

    ``free_text`` must be ``parse_query(...).free_text``, **not** the raw
    request field. Filter operators (``from:``, ``subject:``, ``lang:``) parse
    out of the free text, so ``"subject:invoice"`` is non-blank as a request
    field and blank by the time ``Searcher.search`` tests it — and this
    function's whole job is to ask the question the Searcher will ask. Handed
    the raw string it admits that shape as a keyset continuation, the lexical
    branch declines the cursor, and the Searcher's guard raises where a 400
    was owed.

    ``requested_sort`` is ``None`` when the caller stated no sort. That is
    the documented way to page (send back ``next_cursor``, nothing else), so
    an unstated sort must never out-vote the cursor: the cursor is the only
    statement about ordering in that request, and it was minted by us.

    A *stated* sort the cursor cannot serve raises ``ValidationFailed``.
    Coercing it would ignore the caller silently; honouring it means
    dropping the cursor, which answers a paging request with page 1 of a
    differently ordered search — indistinguishable from a continuation.
    """
    if cursor is None:
        return "fresh"
    if is_keyset_cursor(cursor):
        _reject_sort_mismatch(requested=requested_sort, cursor_sort=KEYSET_SORT)
        if not free_text.strip():
            # The walk rebuilds its FTS predicate from the re-sent query; with
            # none there is nothing to continue, and the Searcher would answer
            # from its empty-query recent-mail branch instead.
            raise ValidationFailed(
                "cursor: this cursor continues a text search; re-send the "
                "original 'query' alongside it"
            )
        return "keyset"
    return "pool"


def reject_pool_sort_mismatch(
    *, requested_sort: SortMode | None, pool_sort: SortMode,
) -> None:
    """Guard the pool kind, whose sort lives in the cached pool, not the cursor.

    ``Searcher.continue_page`` serves whichever sort the pool was minted
    with, so a contradicting stated sort is not applied — and was not
    reported either.
    """
    _reject_sort_mismatch(requested=requested_sort, cursor_sort=pool_sort)


def _reject_sort_mismatch(*, requested: SortMode | None, cursor_sort: SortMode) -> None:
    if requested is not None and requested != cursor_sort:
        raise ValidationFailed(
            f"cursor: this cursor continues a {cursor_sort}-sorted search; "
            f"pass sort={cursor_sort!r} or omit sort (got {requested!r})"
        )
