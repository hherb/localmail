# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Opaque cursor codec for POST /v1/search, and the plan a cursor continues.

Two cursor kinds share the wire field:

  * Pool cursor (hybrid search) — ``"{search_token}:{page}"``.
  * Keyset cursor (date-ordered search) — four spellings, one per
    ``(direction, walk)`` pair: ``K|`` descending/archive, ``KA|``
    ascending/archive, ``KT|`` descending/text, ``KAT|`` ascending/text.
    The payload is the same encoding ``api.browse_cursor`` uses.
    ``_KEYSET_PREFIXES`` below is the table.

Both axes ride in the prefix rather than in the payload so ``K|`` and
``KA|`` keep meaning exactly what they always meant: no cursor in flight
changes meaning, and ``api.browse_cursor``'s shared encoding is untouched.
The prefix is also what the route layer already dispatches on. Clients MUST
treat the cursor as opaque — the prefix is an internal discriminator, not
API.

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
from itertools import product
from typing import Literal, get_args

from localmail.api.browse_cursor import (
    BrowseCursor, decode_browse_cursor, encode_browse_cursor,
)
from localmail.api.errors import ValidationFailed
from localmail.search.keyset_walk import KeysetWalk, keyset_walk_error
from localmail.search.searcher import KeysetCursor
from localmail.search.sort_axes import (
    DEFAULT_SORT,
    DEFAULT_SORT_ORDER,
    TEXTLESS_SORT,
    SortMode,
    SortOrder,
    resolve_sort,
    sort_applicability_error,
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
#:
#: **That leniency cannot be made observable, and it is worth saying why.**
#: The obvious remedy — log when a legacy prefix decodes — does not exist:
#: ``K|`` is not a retired spelling, it is the *live* descending/archive
#: prefix, minted by every blank-query descending walk. A pre-#326 text
#: cursor and an ordinary archive cursor are byte-identical, which is the
#: whole reason the strict reading was rejected. So a log line would fire on
#: the common case and carry no signal about the rare one. The exposure is
#: bounded instead by time: nothing mints a ``K|``-as-text cursor any more,
#: so it ends when the cursors in flight at deploy time expire.
_KEYSET_PREFIXES: tuple[tuple[str, SortOrder, KeysetWalk], ...] = (
    ("KAT|", "asc", "text"),
    ("KA|", "asc", "archive"),
    ("KT|", "desc", "text"),
    ("K|", "desc", "archive"),
)

#: Every ``(order, walk)`` pair must have a prefix, checked at import.
#:
#: ``encode_keyset_cursor`` raises when the table misses a pair, and its
#: comment calls that unreachable "while the table covers the product of
#: both Literals" — which nothing checked. Widening either ``Literal``
#: without widening this table type-checks, imports, and then fails on the
#: **response** path, after the search has already run: `run_search` mints
#: the cursor last, and only ``APIError`` reaches the problem+json handler,
#: so it surfaces as a 500 on a query that succeeded.
#:
#: The same ``get_args`` check ``date_keyset.DATE_ORDER_BY_SQL`` carries for
#: its one-axis table, applied to the two-axis one where the blast radius is
#: worse. Both differences are reported: a *stale* row (a renamed member)
#: fails this too, and a message naming only what is missing would print an
#: empty list for it.
_EXPECTED_KEYSET_PAIRS = set(product(get_args(SortOrder), get_args(KeysetWalk)))
_TABLE_KEYSET_PAIRS = {(order, walk) for _p, order, walk in _KEYSET_PREFIXES}
if _TABLE_KEYSET_PAIRS != _EXPECTED_KEYSET_PAIRS:
    raise RuntimeError(
        "every (order, walk) pair needs a keyset prefix: missing="
        f"{sorted(_EXPECTED_KEYSET_PAIRS - _TABLE_KEYSET_PAIRS)} "
        f"unexpected={sorted(_TABLE_KEYSET_PAIRS - _EXPECTED_KEYSET_PAIRS)}"
    )
if len(_TABLE_KEYSET_PAIRS) != len(_KEYSET_PREFIXES):
    raise RuntimeError("two keyset prefixes claim the same (order, walk) pair")

#: The only sort a keyset cursor can continue — the date-keyset branch is
#: the sole minter and the sole reader of that cursor kind.
#:
#: **Aliased to ``TEXTLESS_SORT`` rather than spelled ``"date"`` again**, so
#: the two cannot drift. They are one fact seen from two ends: the walk a
#: textless query resolves to *is* the walk that mints these cursors. Two
#: independent literals held up two separate properties with nothing checking
#: them — (1) page 1 accepts ``sort=TEXTLESS_SORT`` and mints a keyset cursor
#: that ``_reject_sort_mismatch`` then compares against ``KEYSET_SORT``, so a
#: divergence is #324's own shape (accepted on page 1, refused on page 2);
#: and (2) ``run_search``'s keyset branch used to omit ``SortNotApplicable``
#: from its catch, which was safe only because ``sort_applicability_error``
#: returns ``None`` for ``TEXTLESS_SORT`` — so a divergence turned every
#: keyset continuation of a blank-query walk into a 500. **Property (2) no
#: longer rests on this alias**: since #344 both branches catch the
#: ``SearchArgumentRefused`` family rather than naming members, so the
#: omission it described is not expressible. Property (1) still does, and
#: is why the alias stays.
KEYSET_SORT: SortMode = TEXTLESS_SORT

CursorMode = Literal["fresh", "pool", "keyset"]


@dataclass(frozen=True)
class CursorPlan:
    """Which retrieval mode a request continues, and in what order.

    Returned by :func:`resolve_cursor_plan`. ``sort`` and ``sort_order``
    are what the request was resolved to: the cursor's own for a keyset
    cursor, otherwise the caller's stated values, otherwise the module
    defaults — except that on ``mode="fresh"`` ``sort`` may also be
    ``TEXTLESS_SORT``, derived from the query rather than from either
    (#324), which is neither a stated value nor ``DEFAULT_SORT``.

    They are **not** forwarded to ``Searcher.search`` on the *fresh*
    branch, which passes the caller's raw axes so the Searcher can still
    tell "unstated" from "stated" and resolve them from the composed query
    it alone sees. There they serve this layer's own early refusals. The
    *keyset* branch does forward them, deliberately: the cursor's kind is
    what selects the date path, and any stated value contradicting the
    cursor has already been rejected above.

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
    **only tests**, across the codec, api, MCP and route layers. It has no
    production caller; kept because it is the one reader that answers the
    direction question without decoding a payload.
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
    about what counted as a blank query — so this reads it through the same
    ``sort_axes``/``keyset_walk`` rules the Searcher does. It reads a
    *different string*, though: the raw request field, where the Searcher
    parses the ACL-composed query. They agree for every well-formed query
    and can diverge across an unbalanced quote, which is why the Searcher
    stays the authority and ``run_search`` maps its refusals rather than
    assuming they cannot happen.

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
        # A fresh request's sort is resolved against its own query (#324):
        # a query with no free text has nothing for the hybrid pool to rank
        # against, so the date walk is the only branch that can serve it.
        # Resolving from ``DEFAULT_SORT`` alone made ``run_search``'s
        # rank+asc refusal reason about a path such a request never takes,
        # and made #322's cursor — which records the ordering that *ran* —
        # contradict the ``sort`` its own page 1 had accepted.
        #
        # The same two pure rules ``Searcher.search`` asks, so a refusal
        # cannot be worded differently at the two ends.
        sort_error = sort_applicability_error(requested=requested_sort,
                                              free_text=free_text)
        if sort_error is not None:
            raise ValidationFailed(sort_error)
        return CursorPlan(
            mode="fresh",
            sort=resolve_sort(requested=requested_sort, free_text=free_text),
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
