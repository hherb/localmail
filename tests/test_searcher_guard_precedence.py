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
claimed it also saves a smart-rewrite round trip on a caller error. That
is false and was measured rather than assumed: ``Searcher.search`` runs the
rewriter only under ``parsed.free_text.strip()``, and the walk guard fires
only when that string is *blank*, so no rewrite was ever paid for on this
path. The guard's own docstring claim ("before any connection is opened")
was already true.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.config import SearchConfig
from localmail.search.argument_errors import (
    KeysetCursorUnusable, SortNotApplicable,
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


def test_a_cursor_problem_outranks_the_textless_rule() -> None:
    """The shape the two layers used to answer differently.

    ``KeysetCursorUnusable`` is a strict subclass of nothing the textless
    guard raises, so asserting the *type* is what makes this a precedence
    pin rather than a "some refusal happened" one.
    """
    searcher, pool = _searcher()
    with pytest.raises(KeysetCursorUnusable) as exc:
        searcher.search("", allowed_account_ids=None, sort="rank",
                        keyset_cursor=_TEXT_CURSOR)
    pool.connection.assert_not_called()
    assert not isinstance(exc.value, SortNotApplicable)


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
