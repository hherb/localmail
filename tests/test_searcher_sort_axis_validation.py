# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Both sort axes are membership-checked at runtime (#333 review).

``date_keyset`` reasons explicitly that CI runs no mypy step, so "the value
mypy cannot see — a library caller passing ``"ASC"`` — is caught at runtime
instead". It applied that to ``sort_order`` twice over plus an import-time
completeness check, and to ``sort`` not at all; and its own two checks are
only reachable on the date branch, so the rank branch validated neither.

Both misspellings were silent, and the ``sort`` one was silent twice over:

* ``sort="Date"`` fell through ``== "date"`` into the hybrid branch, so the
  caller got **rank ordering** — and ``next_keyset=None``, so pagination
  stopped after one page.
* ``sort_order="ASC"`` on the rank path missed the exact-match refusal at
  ``effective_sort == "rank" and effective_order == "asc"``, so it was
  neither honoured, nor validated, nor reported — contradicting that guard's
  own docstring, which says a stated parameter the server will not honour is
  reported and never dropped.

Reachable from library and CLI callers only: HTTP and MCP both declare these
as ``Literal``s, which is why this is a loud ``ValueError`` at the boundary
rather than a new wire status.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from localmail.config import SearchConfig
from localmail.search.searcher import Searcher


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
    """A Searcher whose pool raises if touched — the check precedes all IO."""
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("no connection may be opened")
    return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embeddings(),
                    reranker=None, rewriter=None), pool


@pytest.mark.parametrize("sort", ["Date", "DATE", "dat", "relevance", ""])
def test_an_unknown_sort_is_refused_by_name(sort: str) -> None:
    """Never coerced to a default, and never fallen through to rank.

    Falling through is the worse of the two silences: the caller gets an
    ordering they did not ask for *and* a walk that ends after one page,
    both of which read as an archive problem rather than a call-site one.
    """
    searcher, pool = _searcher()
    with pytest.raises(ValueError, match=f"unknown sort {sort!r}"):
        searcher.search("invoice", allowed_account_ids=None, sort=sort)  # type: ignore[arg-type]
    pool.connection.assert_not_called()


@pytest.mark.parametrize("sort_order", ["ASC", "DESC", "ascending", ""])
def test_an_unknown_sort_order_is_refused_on_the_rank_path(
    sort_order: str,
) -> None:
    """The rank branch never reaches ``date_keyset``'s checks.

    ``"ASC"`` is the case that matters: it is not equal to ``"asc"``, so the
    rank+asc refusal does not see it, and the hybrid branch never reads the
    direction at all — so it was served descending with nothing said.
    """
    searcher, pool = _searcher()
    with pytest.raises(ValueError, match=f"unknown sort_order {sort_order!r}"):
        searcher.search("invoice", allowed_account_ids=None, sort="rank",
                        sort_order=sort_order)  # type: ignore[arg-type]
    pool.connection.assert_not_called()


@pytest.mark.parametrize("sort_order", ["ASC", "ascending"])
def test_an_unknown_sort_order_is_refused_on_the_date_path(
    sort_order: str,
) -> None:
    """Already caught by ``date_keyset``, but only after a connection was
    opened and the filters composed. Refusing at the boundary makes the two
    paths agree on both the wording and the cost."""
    searcher, pool = _searcher()
    with pytest.raises(ValueError, match=f"unknown sort_order {sort_order!r}"):
        searcher.search("invoice", allowed_account_ids=None, sort="date",
                        sort_order=sort_order)  # type: ignore[arg-type]
    pool.connection.assert_not_called()


def test_the_stated_defaults_still_pass() -> None:
    """The positive control: a check that refused everything would satisfy
    every assertion above. Both axes stated explicitly, and both omitted."""
    for kwargs in ({"sort": "rank", "sort_order": "desc"},
                   {"sort": "date", "sort_order": "asc"},
                   {}):
        searcher, pool = _searcher()
        with pytest.raises(AssertionError, match="no connection"):
            searcher.search("invoice", allowed_account_ids=None, **kwargs)  # type: ignore[arg-type]
        pool.connection.assert_called_once()


@pytest.mark.parametrize("sort", ["Date", "rnk", ""])
def test_an_unknown_sort_is_refused_on_a_textless_query_too(sort: str) -> None:
    """Since #324 a textless query resolves to ``TEXTLESS_SORT`` whatever
    arrived, so a misspelling would be swallowed on exactly the branch that
    used to swallow it — served date-ordered, silently, as if the caller had
    asked for it.

    That is why the membership check reads the value as **stated**, not as
    resolved. Every case above uses a query with free text, so none of them
    reaches this branch.
    """
    searcher, pool = _searcher()
    with pytest.raises(ValueError, match=f"unknown sort {sort!r}"):
        searcher.search("", allowed_account_ids=None, sort=sort)  # type: ignore[arg-type]
    pool.connection.assert_not_called()
