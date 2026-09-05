# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A cursor problem outranks the textless rule — at both layers (#344).

``test_api_search_rank_without_text.py`` states the rule as a rule: the
cursor guard's message "is the more specific diagnosis and must not be
displaced by the textless one". It was enforced at the api boundary and
**inverted inside the Searcher**, where ``sort_applicability_error`` ran
ahead of the walk guard — so one shape got the cursor diagnosis over HTTP
and the textless one from a library call:

    search("", keyset_cursor=<text-walk>, sort="rank")

Both are true of that request and both recommend the same remedy, so the
cost was never a wrong answer. It is the two-layers-wording-one-rule-
differently shape this cluster keeps filing, and it was untested in either
direction inside the Searcher — which is why it survived review of the PR
that created it.

**What moving the guard does not buy.** An earlier draft of this change
claimed it also saves a smart-rewrite round trip on a caller error. That is
false: ``Searcher.search`` runs the rewriter only under
``parsed.free_text.strip()``, and the walk guard fires only when that string
is *blank*, so no rewrite was ever paid for on this path — before the move or
after it. The guard's own docstring claim ("before any connection is opened")
was already true.

The refutation is **asserted rather than argued** —
``test_the_reorder_buys_no_rewrite_round_trip`` hands the Searcher a rewriter
that raises if touched — because "measured" is a word this tree reserves for
something it has actually run, and the first draft of this docstring used it
for a code reading.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.config import SearchConfig
from localmail.search.argument_errors import (
    KeysetCursorUnusable, KeysetOrderMismatch, SearchArgumentRefused,
    SortNotApplicable,
)
from localmail.search.searcher import KeysetCursor, Searcher


class _Embeddings:
    name = "s"
    model = "s"
    dimension = 768

    def embed_documents(self, texts):  # pragma: no cover - never reached
        raise AssertionError("retrieval must not start")

    def embed_query(self, text):  # pragma: no cover - never reached
        raise AssertionError("retrieval must not start")

    def health_check(self) -> None:
        pass


def _searcher() -> tuple[Searcher, MagicMock]:
    """A Searcher whose pool raises if touched — every guard precedes IO."""
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("no connection may be opened")
    return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embeddings(),
                    reranker=None, rewriter=None), pool


_TEXT_CURSOR = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc),
                            id=100, order="desc", walk="text")
_ARCHIVE_CURSOR = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc),
                               id=100, order="desc", walk="archive")
_ASCENDING_CURSOR = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc),
                                 id=100, order="asc", walk="archive")


class _ExplodingRewriter:
    """A rewriter that fails the test if the smart path is ever entered.

    ``name``/``model`` satisfy the ``QueryRewriter`` protocol; the Searcher
    reads them only when a rewrite is reported, which must never happen here.
    """

    name = "s"
    model = "s"

    def rewrite(self, text: str):  # pragma: no cover - never reached
        raise AssertionError("no rewrite may be attempted")


def test_a_cursor_problem_outranks_the_textless_rule() -> None:
    """The shape the two layers used to answer differently.

    Naming the *type* in ``pytest.raises`` is the whole pin, and it is
    sufficient: with the walk guard back below the sort guards this shape
    raises ``SortNotApplicable``, which ``pytest.raises`` rejects. An
    ``assert not isinstance(exc.value, SortNotApplicable)`` used to sit here
    arguing that role; it was vacuous, the two being siblings, so no change
    that keeps them siblings could fail it.
    """
    searcher, pool = _searcher()
    with pytest.raises(KeysetCursorUnusable, match="continues a text search"):
        searcher.search("", allowed_account_ids=None, sort="rank",
                        keyset_cursor=_TEXT_CURSOR)
    pool.connection.assert_not_called()


def test_the_textless_rule_still_fires_when_no_cursor_competes() -> None:
    """The positive control for the *other* guard.

    A move that hoisted the walk guard so far it swallowed the textless
    rule outright would leave the assertion above passing and #324
    unenforced.
    """
    searcher, pool = _searcher()
    with pytest.raises(SortNotApplicable, match="no free text"):
        searcher.search("", allowed_account_ids=None, sort="rank")
    pool.connection.assert_not_called()


def test_an_archive_cursor_leaves_the_textless_rule_in_charge() -> None:
    """An archive-walk cursor pages under any query (#322/#326), so it is
    not a cursor *problem* — and must not shadow the textless refusal."""
    searcher, _ = _searcher()
    with pytest.raises(SortNotApplicable, match="no free text"):
        searcher.search("", allowed_account_ids=None, sort="rank",
                        keyset_cursor=_ARCHIVE_CURSOR)


def test_a_text_cursor_with_its_query_back_reaches_retrieval() -> None:
    """The positive control for the walk guard itself: re-sending the query
    is exactly what #326 asks of a paging caller, and must not be refused."""
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search("invoice", allowed_account_ids=None, sort="date",
                        keyset_cursor=_TEXT_CURSOR)
    pool.connection.assert_called()


def test_the_order_mismatch_guard_outranks_every_other() -> None:
    """``KeysetOrderMismatch``'s raise site, which nothing exercised (#344).

    It is decided above ``parse_query``, so it outranks both guards this file
    is otherwise about — and until now it was reachable only in theory from a
    library caller: replacing its condition with ``if False:`` left the
    **entire** suite green (3180 passed, i.e. every test that existed before
    this one). Its wire-layer sibling
    (``_reject_order_mismatch``) is covered end to end in
    ``test_serve_search_route.py``, which is why the library half went
    unnoticed.

    Stated *and* contradicting is the only shape refused; the request below
    also has no free text and a ``rank`` sort, so it satisfies the textless
    rule too, which is what makes this a precedence assertion rather than
    merely a raise-site one.
    """
    searcher, pool = _searcher()
    with pytest.raises(KeysetOrderMismatch, match="contradicts the cursor"):
        searcher.search("", allowed_account_ids=None, sort="rank",
                        sort_order="desc", keyset_cursor=_ASCENDING_CURSOR)
    pool.connection.assert_not_called()


def test_an_omitted_sort_order_inherits_the_cursor_rather_than_refusing() -> None:
    """The positive control: only a *stated* contradiction is refused.

    A guard that fired on the cursor's direction alone would break the paging
    idiom the docs prescribe — state the order once, then send only the
    cursor back — which is the defect ``KeysetCursor.order`` was added for.
    """
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search("invoice", allowed_account_ids=None, sort="date",
                        keyset_cursor=_ASCENDING_CURSOR)
    pool.connection.assert_called()


def test_the_reorder_buys_no_rewrite_round_trip() -> None:
    """The refuted claim, asserted instead of argued (see the module docstring).

    Hoisting the walk guard was said to save a smart rewrite on a caller
    error. It does not, and the reason is not the hoist: the rewriter is
    gated on ``parsed.free_text.strip()`` and this guard fires only when that
    string is blank, so the two are mutually exclusive on *either* ordering.
    Pinning it here keeps the docstring's correction honest, and would catch a
    future change that moved the rewrite above the guard.
    """
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("no connection may be opened")
    searcher = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embeddings(),
                        reranker=None, rewriter=_ExplodingRewriter())
    with pytest.raises(KeysetCursorUnusable):
        searcher.search("", allowed_account_ids=None, sort="rank",
                        keyset_cursor=_TEXT_CURSOR, smart=True)
    pool.connection.assert_not_called()


# Every row must reach a *cursor* guard once the membership check is removed,
# or it pins something else under this test's name. Row 0 shipped as
# ``("sort", "Date", _ASCENDING_CURSOR)`` and reached none: with a blank query
# ``resolve_sort`` returns ``date`` so the hybrid guard cannot fire, the walk
# is ``archive`` so #326's cannot, and ``KeysetOrderMismatch`` needs a stated
# ``sort_order``. It pinned membership above ``sort_applicability_error`` —
# true, and not what the name claims. Measured against three positional
# mutations; every row below fails at least one.
@pytest.mark.parametrize("axis,bad,cursor", [
    ("sort", "Date", _TEXT_CURSOR),
    ("sort", "rank ", _TEXT_CURSOR),
    ("sort_order", "ASC", _ASCENDING_CURSOR),
    ("sort_order", "DESC", _TEXT_CURSOR),
])
def test_membership_outranks_every_cursor_guard(
    axis: str, bad: str, cursor: KeysetCursor,
) -> None:
    """A value that is not a value cannot contradict a cursor (#348).

    ``sort_order="ASC"`` against an ascending cursor used to raise
    ``KeysetOrderMismatch`` — "contradicts the cursor, which continues an
    ascending walk; pass sort_order='asc'" — which is a true sentence about
    a caller whose actual fault is a typo, and sends them to their paging
    logic. It is also a coincidence of the cursor in hand: the same typo
    against a descending cursor recommends ``'desc'``.

    ``resolve_cursor_plan`` orders these the same way at the api boundary,
    so the two layers cannot answer one input differently — the shape this
    whole file exists to pin.
    """
    searcher, pool = _searcher()
    with pytest.raises(ValueError, match=f"unknown {axis} {bad!r}"):
        searcher.search("", allowed_account_ids=None,
                        keyset_cursor=cursor,
                        **{axis: bad})  # type: ignore[arg-type]
    pool.connection.assert_not_called()


@pytest.mark.parametrize("axis,bad", [("sort", "Date"), ("sort_order", "ASC")])
def test_a_membership_refusal_is_not_a_search_argument_refused(
    axis: str, bad: str,
) -> None:
    """The operator decision #348 asked to be recorded, asserted (#344).

    A membership error is a **type** error a well-typed caller cannot make,
    where every ``SearchArgumentRefused`` member is a **cross-argument**
    error a well-typed caller makes routinely. And since ``run_search`` now
    refuses these at the boundary, membership would be **inert at both catch
    sites** — the catch could never see one.

    Not, as an earlier wording had it, that the family would gain a member
    "no wire caller can reach": the Searcher is public API and
    ``SortNotApplicable``'s own docstring names library callers as its
    audience, which is the whole reason this check exists.

    Pinned rather than left to the docstring because both are ``ValueError``
    subclasses, so nothing else here would notice the reclassification.
    """
    searcher, _ = _searcher()
    with pytest.raises(ValueError) as exc:
        searcher.search("invoice", allowed_account_ids=None,
                        **{axis: bad})  # type: ignore[arg-type]
    assert not isinstance(exc.value, SearchArgumentRefused)
