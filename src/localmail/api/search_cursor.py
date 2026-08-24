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
from localmail.search.keyset_walk import KeysetWalk, keyset_walk_error
from localmail.search.searcher import (
    DEFAULT_SORT,
    DEFAULT_SORT_ORDER,
    KeysetCursor,
    SortMode,
    SortOrder,
)

#: One prefix per (direction, walk) pair, spelled ``K`` + ``A`` when
#: ascending + ``T`` when the walk carries free text + the terminator.
#:
#: The ``|`` terminator is what keeps them disjoint: every prefix ends in
#: it and contains no other, so a shorter one can never match inside a
#: longer one ("KAT|" does not start with "KA|", nor "KT|" with "K|").
#: No scan order can therefore misclassify a cursor. Longest first is a
#: prefix table's convention, not a correctness requirement; it becomes one
#: the day a prefix is added that does not end in the terminator — which is
#: why ``test_no_keyset_prefix_is_a_prefix_of_another`` asserts the
#: property rather than leaving it to this comment.
#:
#: ``K|`` and ``KA|`` keep the meanings they shipped with, so no cursor in
#: flight changes direction. They read as ``archive`` — the lenient half of
#: the #326 rule — because a legacy cursor could have come from either
#: walk: archive leaves that one paging session un-checked, while text
#: would manufacture a 400 for a caller correctly paging a blank-query
#: walk, breaking a feature that shipped the same week.
_KEYSET_PREFIXES: tuple[tuple[str, SortOrder, KeysetWalk], ...] = (
    ("KAT|", "asc", "text"),
    ("KA|", "asc", "archive"),
    ("KT|", "desc", "text"),
    ("K|", "desc", "archive"),
)

#: The only sort a keyset cursor can continue — the date-keyset branch is
#: the sole minter and the sole reader of that cursor kind.
KEYSET_SORT: SortMode = "date"

CursorMode = Literal["fresh", "pool", "keyset"]


@dataclass(frozen=True)
class CursorPlan:
    """Which retrieval mode a request continues, and in what order.

    Returned by :func:`resolve_cursor_plan`. ``sort`` and ``sort_order``
    are what the request was resolved to: the cursor's own for a keyset
    cursor, otherwise the caller's stated values, otherwise the module
    defaults.

    For ``mode="pool"`` they are **not** what the request will run with.
    ``continue_page`` serves whatever ordering the pool was built with, so
    these carry the caller's statement (or a default) *about* that pool,
    which is only ever a claim to be checked — and is why
    ``_check_pool_sort`` compares the raw arguments against the pool's own
    metadata rather than anything here.

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


def encode_keyset_cursor(ks: KeysetCursor) -> str:
    """Mint a keyset cursor that carries the direction it continues.

    The direction is read off ``ks``, where ``_date_keyset_search`` stamped
    it from the walk that produced the rows. It used to be a second
    argument, supplied by the api layer from its own resolved plan — which
    was correct only for as long as that plan and the walk could not
    disagree. Taking it from the cursor makes minting a descending cursor
    for an ascending walk unrepresentable rather than merely discouraged.
    """
    payload = encode_browse_cursor(BrowseCursor(ts=ks.ts, id=ks.id))
    for prefix, order, walk in _KEYSET_PREFIXES:
        if order == ks.order and walk == ks.walk:
            return f"{prefix}{payload}"
    # Unreachable while the table covers the product of both Literals, and
    # a raise rather than a fallback so widening either axis without
    # widening the table cannot mint a cursor that decodes as something
    # else. Named, not an assert: asserts vanish under ``python -O``.
    raise ValueError(
        f"no keyset prefix for order={ks.order!r} walk={ks.walk!r}"
    )


def is_keyset_cursor(raw: str) -> bool:
    return any(raw.startswith(p) for p, _, _ in _KEYSET_PREFIXES)


def keyset_order(raw: str) -> SortOrder:
    """The direction a keyset cursor continues, from its prefix alone.

    A cursor minted before the ascending prefix existed carries ``K|`` and
    is descending, which is what it has always meant — so no cursor in
    flight changes meaning.

    Production reads the whole cursor (``decode_keyset_cursor``) because it
    needs the position and the walk as well. This stays as the cheap
    payload-free accessor for callers that only want the direction — today
    the route-level tests, which assert it without caring what a cursor's
    payload or walk happens to be.
    """
    for prefix, order, _walk in _KEYSET_PREFIXES:
        if raw.startswith(prefix):
            return order
    raise ValidationFailed(f"cursor: not a keyset cursor: {raw!r}")


def decode_keyset_cursor(raw: str) -> KeysetCursor:
    """Recover the position *and* the direction the prefix encodes.

    The direction travels on the cursor rather than beside it, so the
    Searcher cannot be handed a position without the sense in which to
    read it — see ``KeysetCursor.order``.
    """
    for prefix, order, walk in _KEYSET_PREFIXES:
        if raw.startswith(prefix):
            bc = decode_browse_cursor(raw[len(prefix):])
            return KeysetCursor(ts=bc.ts, id=bc.id, order=order, walk=walk)
    raise ValidationFailed(f"cursor: not a keyset cursor: {raw!r}")


def resolve_cursor_plan(
    *,
    cursor: str | None,
    requested_sort: SortMode | None,
    requested_sort_order: SortOrder | None,
    free_text: str,
) -> CursorPlan:
    """Decide the retrieval mode and both ordering axes — cursor first.

    ``free_text`` must be ``parse_query(...).free_text``, not the caller's
    raw query field: the filter operators are lifted out by then, and
    ``subject:invoice`` leaves no free text behind for an FTS predicate to
    be rebuilt from. Two predicates for one rule is what produced #308's
    follow-up defect — the api gate and the retrieval branch disagreeing
    about what counted as a blank query — so this asks
    ``keyset_walk.walk_for_text``, exactly as the branch does.

    It is an input again, but for a narrower question than before #322.
    The old guard refused *any* keyset cursor presented with a blank query,
    because the blank-query branch dropped the cursor and answered with its
    own page 1; that branch paginates now, so the premise is gone and the
    guard would forbid the pagination it gained. What comes back (#326) is
    the one pair that guard also happened to catch: a cursor from the
    **text** walk, whose FTS predicate the next page must rebuild from the
    re-sent query. An archive-walk cursor still continues under any query.

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
        # Decoded rather than prefix-scanned because the walk is needed
        # below. ``run_search`` decodes again for the position it passes on;
        # the two cannot disagree (pure function, same string), and the
        # duplicate is a base64 read. Deliberately *not* hoisted onto
        # ``CursorPlan`` — that type already carries two fields its
        # pool-mode consumer must ignore (#327), and a third would be one
        # more.
        #
        # Decoding here also moves a malformed *payload* from ``run_search``
        # to ahead of the empty-ACL short-circuit, which is where this
        # module says such a request belongs: that branch answers with an
        # empty page, indistinguishable from "you have reached the end".
        ks = decode_keyset_cursor(cursor)
        _reject_sort_mismatch(requested=requested_sort, cursor_sort=KEYSET_SORT)
        _reject_order_mismatch(requested=requested_sort_order,
                               cursor_order=ks.order)
        walk_error = keyset_walk_error(cursor_walk=ks.walk, free_text=free_text)
        if walk_error is not None:
            raise ValidationFailed(f"cursor: {walk_error}")
        return CursorPlan(mode="keyset", sort=KEYSET_SORT, sort_order=ks.order)
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
