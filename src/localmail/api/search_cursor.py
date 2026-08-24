# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Opaque cursor codec for POST /v1/search, and the plan a cursor continues.

Two cursor kinds share the wire field:

  * Pool cursor (hybrid search) — ``"{search_token}:{page}"``.
  * Keyset cursor (date-ordered search) — ``"K|<base64>"`` descending,
    ``"KA|<base64>"`` ascending, where the payload is the same encoding
    ``api.browse_cursor`` uses.

The direction rides in the prefix rather than the payload so ``K|`` keeps
meaning exactly what it always meant: no cursor in flight changes meaning,
and ``api.browse_cursor``'s shared encoding is untouched. The prefix is
also what the route layer already dispatches on. Clients MUST treat the
cursor as opaque — the prefix is an internal discriminator, not API.

Minting, matching, and *interpreting* live together here because writing
them apart is what produced the defect this module's ``resolve_cursor_plan``
now rules out (#308): the request's ``sort`` was resolved to its default
before the cursor was read, and a cursor the resulting mode could not read
was dropped in silence. ``sort_order`` is the same defect on a second axis
— a cursor carrying only ``(ts, id)`` would continue an ascending walk
backwards — which is why the two axes are decided here, together.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from localmail.api.browse_cursor import (
    BrowseCursor, decode_browse_cursor, encode_browse_cursor,
)
from localmail.api.errors import ValidationFailed
from localmail.search.searcher import (
    DEFAULT_SORT,
    DEFAULT_SORT_ORDER,
    KeysetCursor,
    SortMode,
    SortOrder,
)

_KEYSET_PREFIX_DESC = "K|"
_KEYSET_PREFIX_ASC = "KA|"

#: Longest first: "KA|" also starts with "K", so a shortest-first scan
#: would classify every ascending cursor as descending.
_KEYSET_PREFIXES: tuple[tuple[str, SortOrder], ...] = (
    (_KEYSET_PREFIX_ASC, "asc"),
    (_KEYSET_PREFIX_DESC, "desc"),
)

#: The only sort a keyset cursor can continue — the date-keyset branch is
#: the sole minter and the sole reader of that cursor kind.
KEYSET_SORT: SortMode = "date"

CursorMode = Literal["fresh", "pool", "keyset"]


@dataclass(frozen=True)
class CursorPlan:
    """Which retrieval mode a request continues, and in what order.

    Returned by :func:`resolve_cursor_plan`. ``sort`` and ``sort_order``
    are the **resolved** values the request will actually run with: the
    cursor's own when it has one, the caller's when there is no cursor,
    the module defaults when neither states anything.

    One object rather than one function per axis. Two predicates for one
    rule is what produced the #308 follow-up defect, where the api gate
    and the retrieval branch disagreed about what counted as a blank
    query — so the axes are decided together or not at all.
    """
    mode: CursorMode
    sort: SortMode
    sort_order: SortOrder


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


def encode_keyset_cursor(ks: KeysetCursor, order: SortOrder) -> str:
    """Mint a keyset cursor that carries the direction it continues.

    ``order`` is required, not defaulted: a forgotten argument would mint
    a descending cursor for an ascending walk, which is the exact silent
    reversal this parameter exists to make impossible.
    """
    payload = encode_browse_cursor(BrowseCursor(ts=ks.ts, id=ks.id))
    prefix = _KEYSET_PREFIX_ASC if order == "asc" else _KEYSET_PREFIX_DESC
    return f"{prefix}{payload}"


def is_keyset_cursor(raw: str) -> bool:
    return any(raw.startswith(p) for p, _ in _KEYSET_PREFIXES)


def keyset_order(raw: str) -> SortOrder:
    """The direction a keyset cursor continues.

    A cursor minted before the ascending prefix existed carries ``K|`` and
    is descending, which is what it has always meant — so no cursor in
    flight changes meaning.
    """
    for prefix, order in _KEYSET_PREFIXES:
        if raw.startswith(prefix):
            return order
    raise ValidationFailed(f"cursor: not a keyset cursor: {raw!r}")


def decode_keyset_cursor(raw: str) -> KeysetCursor:
    for prefix, _ in _KEYSET_PREFIXES:
        if raw.startswith(prefix):
            bc = decode_browse_cursor(raw[len(prefix):])
            return KeysetCursor(ts=bc.ts, id=bc.id)
    raise ValidationFailed(f"cursor: not a keyset cursor: {raw!r}")


def resolve_cursor_plan(
    *,
    cursor: str | None,
    requested_sort: SortMode | None,
    requested_sort_order: SortOrder | None,
    free_text: str,
) -> CursorPlan:
    """Decide the retrieval mode and both ordering axes — cursor first.

    ``free_text`` must be ``parse_query(...).free_text``, **not** the raw
    request field: filter operators parse out of the free text, so
    ``"subject:invoice"`` is non-blank as a request field and blank by the
    time ``Searcher.search`` tests it, and this function's job is to ask
    the question the Searcher will ask.

    A ``None`` on either axis means the caller stated nothing. That is the
    documented way to page, so an unstated value never out-votes the
    cursor: the cursor is the only statement about ordering in such a
    request, and it was minted by us.

    A *stated* value the cursor cannot serve raises ``ValidationFailed``.
    Coercing it would ignore the caller silently; honouring it means
    dropping the cursor, which answers a paging request with page 1 of a
    differently ordered search.
    """
    if cursor is None:
        return CursorPlan(
            mode="fresh",
            sort=DEFAULT_SORT if requested_sort is None else requested_sort,
            sort_order=(DEFAULT_SORT_ORDER if requested_sort_order is None
                        else requested_sort_order),
        )
    if is_keyset_cursor(cursor):
        order = keyset_order(cursor)
        _reject_sort_mismatch(requested=requested_sort, cursor_sort=KEYSET_SORT)
        _reject_order_mismatch(requested=requested_sort_order, cursor_order=order)
        # A blank query is allowed: the blank-query branch reads the cursor
        # too since it gained pagination, so refusing would forbid exactly
        # that paging. The cursor carries a position, never a query — the
        # "send the same query and filters" contract is unchanged, and it
        # already governs every filter equally.
        return CursorPlan(mode="keyset", sort=KEYSET_SORT, sort_order=order)
    return CursorPlan(
        mode="pool",
        sort=DEFAULT_SORT if requested_sort is None else requested_sort,
        sort_order=(DEFAULT_SORT_ORDER if requested_sort_order is None
                    else requested_sort_order),
    )


def reject_pool_sort_mismatch(
    *,
    requested_sort: SortMode | None,
    requested_sort_order: SortOrder | None,
    pool_sort: SortMode,
    pool_sort_order: SortOrder,
) -> None:
    """Guard both axes of the pool kind, whose ordering lives in the pool.

    ``Searcher.continue_page`` serves whatever the pool was minted with, so
    a contradicting stated value is not applied — and was not reported
    either, until #308.
    """
    _reject_sort_mismatch(requested=requested_sort, cursor_sort=pool_sort)
    _reject_order_mismatch(requested=requested_sort_order,
                           cursor_order=pool_sort_order)


def _reject_sort_mismatch(*, requested: SortMode | None, cursor_sort: SortMode) -> None:
    if requested is not None and requested != cursor_sort:
        raise ValidationFailed(
            f"cursor: this cursor continues a {cursor_sort}-sorted search; "
            f"pass sort={cursor_sort!r} or omit sort (got {requested!r})"
        )


def _reject_order_mismatch(
    *, requested: SortOrder | None, cursor_order: SortOrder,
) -> None:
    if requested is not None and requested != cursor_order:
        raise ValidationFailed(
            f"cursor: this cursor continues a {cursor_order}ending search; "
            f"pass sort_order={cursor_order!r} or omit sort_order "
            f"(got {requested!r})"
        )
