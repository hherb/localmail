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
    search the GUI issues."""
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search(query, allowed_account_ids=None)
    pool.connection.assert_called_once()


@pytest.mark.parametrize("query", TEXTLESS_QUERIES)
def test_a_stated_date_without_free_text_reaches_the_date_walk(
    query: str,
) -> None:
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search(query, allowed_account_ids=None, sort="date")
    pool.connection.assert_called_once()


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
