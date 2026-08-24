# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""``sort_order="asc"`` is refused for ``sort="rank"``, before any IO.

The rank path serves a bounded candidate pool, so reversing it returns the
least relevant of the *top hits* rather than of the archive — an answer
that looks meaningful and is an artifact of where the pool stopped. We
cannot serve the question honestly, so we decline it rather than ignore it
(#308/#312: a stated parameter the server will not honour is reported).

The guard lives in the Searcher as well as in api/ because the CLI and
library callers reach it without passing through the HTTP layer — the same
reason ``KeysetCursorUnusable`` is guarded twice.
"""
from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.search.searcher import (
    DEFAULT_SORT_ORDER,
    Searcher,
    SortOrderNotApplicable,
)


class _Embeddings:
    name = "s"; model = "s"; dimension = 768

    def embed_documents(self, texts):  # pragma: no cover - never reached
        raise AssertionError("retrieval must not start")

    def embed_query(self, text):  # pragma: no cover - never reached
        raise AssertionError("retrieval must not start")

    def health_check(self) -> None:
        pass


def _searcher():
    """A Searcher whose pool raises if touched — the guard precedes all IO."""
    from unittest.mock import MagicMock
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("no connection may be opened")
    return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embeddings(),
                    reranker=None, rewriter=None), pool


def test_the_default_order_is_descending() -> None:
    assert DEFAULT_SORT_ORDER == "desc"


def test_rank_with_ascending_order_is_refused_before_any_io() -> None:
    searcher, pool = _searcher()
    with pytest.raises(SortOrderNotApplicable) as exc:
        searcher.search("invoice", allowed_account_ids=None, sort="rank",
                        sort_order="asc")
    pool.connection.assert_not_called()
    assert "sort='date'" in str(exc.value), (
        "the message must name the remedy: a caller who sent sort_order "
        "alone needs to be told which sort serves it"
    )


def test_an_unstated_sort_is_rank_and_is_refused_the_same_way() -> None:
    """`sort_order="asc"` alone resolves `sort` to rank, so it is refused.

    This is the shape a caller reaches for first, so the refusal has to
    cover it — a guard reading only an explicitly stated `sort` misses it.
    """
    searcher, pool = _searcher()
    with pytest.raises(SortOrderNotApplicable):
        searcher.search("invoice", allowed_account_ids=None, sort_order="asc")
    pool.connection.assert_not_called()


def test_rank_with_descending_order_is_accepted() -> None:
    """Only `asc` is refused. "Descending relevance" is what rank serves."""
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search("invoice", allowed_account_ids=None, sort="rank",
                        sort_order="desc")
    pool.connection.assert_called_once()


def test_date_with_ascending_order_reaches_retrieval() -> None:
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search("invoice", allowed_account_ids=None, sort="date",
                        sort_order="asc")
    pool.connection.assert_called_once()
