# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""``sort="rank"`` is refused for a query with no free text (#324).

The Searcher's own guard, which is the whole of the protection for CLI and
library callers — ``api.run_search`` refuses the same shape at its boundary,
but those callers never pass through it. The sibling ``KeysetCursorUnusable``
and ``SortOrderNotApplicable`` guards exist for exactly this reason.

Both faces of #324 are pinned here, because they are one rule read from two
ends and a fix to either alone is wrong:

* a **stated** ``rank`` on a textless query is refused, where it used to be
  silently served date-ordered and then contradicted by its own cursor;
* an **unstated** sort on the same query resolves to ``date``, so
  ``sort_order="asc"`` is *honoured* rather than refused by a rank+asc guard
  reasoning about a path the request would never have taken.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from localmail.config import SearchConfig
from localmail.search.searcher import (
    Searcher,
    SortNotApplicable,
    SortOrderNotApplicable,
)


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
    """A Searcher whose pool raises if touched — the guard precedes all IO."""
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("no connection may be opened")
    return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embeddings(),
                    reranker=None, rewriter=None), pool


#: Queries that reach the date walk because they carry no free text. The
#: operator-only spellings are the ones a client has no cue about: they are
#: non-empty *request fields* that ``parse_query`` reduces to nothing, which
#: is the same trap the #308 follow-up defect turned on.
TEXTLESS_QUERIES = ("", "   ", "subject:invoice", 'from:"alice@example.com"',
                    "has:attachment", "lang:en", "account_id:1")


def _assert_takes_the_date_walk(query: str, **kw) -> None:
    """Prove the *date-keyset* branch ran, rather than merely that IO began.

    The two retrieval branches are indistinguishable from the pool — both
    open a connection — so a ``pool.connection.assert_called_once()`` stays
    green when the query is routed into the hybrid pool instead, which is
    exactly the regression the callers of this helper claim to catch.

    The branch is therefore observed at the one method only the date walk
    calls. The pool is left working so that method is reached, and the two
    reads it makes first are stubbed; the hybrid branch would instead reach
    ``_Embeddings.embed_query`` and raise "retrieval must not start", so the
    two outcomes are told apart by message rather than by both being IO.
    """
    pool = MagicMock()
    searcher = Searcher(pool=pool, cfg=SearchConfig(),
                        embeddings=_Embeddings(), reranker=None,
                        rewriter=None)
    searcher._resolve_account_names = (  # type: ignore[method-assign]
        lambda conn, parsed: parsed)
    searcher._maybe_warn_unpopulated_body_lang = (  # type: ignore[method-assign]
        lambda conn, parsed: None)
    searcher._date_keyset_search = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("the date walk ran"))
    with pytest.raises(AssertionError, match="the date walk ran"):
        searcher.search(query, allowed_account_ids=None, **kw)


@pytest.mark.parametrize("query", TEXTLESS_QUERIES)
def test_a_stated_rank_without_free_text_is_refused_before_any_io(
    query: str,
) -> None:
    searcher, pool = _searcher()
    with pytest.raises(SortNotApplicable) as exc:
        searcher.search(query, allowed_account_ids=None, sort="rank")
    pool.connection.assert_not_called()
    assert "sort='date'" in str(exc.value)


def test_the_refusal_is_a_named_subclass_not_a_bare_value_error() -> None:
    """So a boundary can map exactly this to a 400 without also catching
    what psycopg, ``datetime`` and the embedding backends raise."""
    assert issubclass(SortNotApplicable, ValueError)
    assert SortNotApplicable is not ValueError


@pytest.mark.parametrize("query", TEXTLESS_QUERIES)
def test_an_unstated_sort_without_free_text_reaches_the_date_walk(
    query: str,
) -> None:
    """The positive control. A guard that refused every textless query
    would satisfy the refusal test above and break every filter-only
    search the GUI issues.

    Asserted on ``_date_keyset_search`` itself, not on the pool: **both**
    branches open a connection, so a pool assertion cannot see which one
    ran and stays green even when the query is routed into the hybrid
    pool — which is precisely the regression this test's name claims to
    catch.
    """
    _assert_takes_the_date_walk(query)


@pytest.mark.parametrize("query", TEXTLESS_QUERIES)
def test_a_stated_date_without_free_text_reaches_the_date_walk(
    query: str,
) -> None:
    _assert_takes_the_date_walk(query, sort="date")


@pytest.mark.parametrize("query", TEXTLESS_QUERIES)
def test_ascending_order_without_free_text_is_now_honoured(query: str) -> None:
    """#324's inverse face.

    ``sort_order='asc'`` with no stated sort used to be a 400 naming
    ``sort='rank'`` — a path the request would never have taken, since a
    textless query has always been served by the date walk. The rank+asc
    guard reasons from the branch that will serve now, so the request runs.
    """
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search(query, allowed_account_ids=None, sort_order="asc")
    pool.connection.assert_called_once()


def test_ascending_order_with_free_text_is_still_refused() -> None:
    """The other half of that control: nothing about the rank path moved."""
    searcher, pool = _searcher()
    with pytest.raises(SortOrderNotApplicable):
        searcher.search("invoice", allowed_account_ids=None, sort_order="asc")
    pool.connection.assert_not_called()


def test_a_stated_rank_with_free_text_still_reaches_the_pool() -> None:
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search("invoice", allowed_account_ids=None, sort="rank")
    pool.connection.assert_called_once()


def test_the_refused_shape_is_judged_after_the_acl_scope_is_composed() -> None:
    """An ACL-scoped call composes ``account_id:`` tokens into the query,
    and those are operators — so an ACL must not turn a textless query into
    a text one. Pinned because the Searcher reads the *composed* string,
    which is what ``api.run_search`` hands it."""
    searcher, pool = _searcher()
    with pytest.raises(SortNotApplicable):
        searcher.search("account_id:1 account_id:2", allowed_account_ids=[1, 2],
                        sort="rank")
    pool.connection.assert_not_called()
