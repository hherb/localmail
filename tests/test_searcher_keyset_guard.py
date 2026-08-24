# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""``Searcher.search`` may not accept a keyset cursor it will not read (#308).

``keyset_cursor`` is consumed by the date-ordered keyset walk (``sort=
"date"``, or any blank query). The hybrid pool branch is now the only one
that does not read it — it used to ignore it and answer with its own page
1, which reads as a continuation and is a restart. The api/ layer is fixed
to never send an unusable one; this is the guard that makes the drop
impossible for every other caller (CLI, library, a future transport).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.config import SearchConfig
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
    """A Searcher whose pool raises if touched — the guard precedes all IO."""
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("no connection may be opened")
    return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embeddings(),
                    reranker=None, rewriter=None), pool


_CURSOR = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100)


def test_rank_sort_rejects_a_keyset_cursor_instead_of_dropping_it() -> None:
    searcher, _ = _searcher()
    with pytest.raises(ValueError, match="keyset_cursor"):
        searcher.search("invoice", allowed_account_ids=None, sort="rank",
                        keyset_cursor=_CURSOR)


def test_an_empty_query_now_reads_the_keyset_cursor() -> None:
    """The blank-query branch honours the cursor, so it must not be refused.

    It used to be refused because that branch dropped the cursor and
    answered with its own page 1. Now it continues the walk at the right
    position, and refusing would forbid the paging that change adds. The
    Searcher's pool raises on touch, so reaching retrieval is the assertion.
    """
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search("", allowed_account_ids=None, sort="date",
                        keyset_cursor=_CURSOR)
    pool.connection.assert_called_once()


def test_a_search_without_a_keyset_cursor_is_unaffected() -> None:
    """The guard must not fire on the ordinary path — it would break every
    rank search, which is the one shape a wrongly-placed raise would miss."""
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection"):
        searcher.search("invoice", allowed_account_ids=None, sort="rank")
    pool.connection.assert_called_once()
