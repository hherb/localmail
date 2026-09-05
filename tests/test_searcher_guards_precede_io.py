# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Every ``SearchArgumentRefused`` is raised before any IO (#349).

``api/search.py`` catches the family at two sites, and each ``try`` wraps the
**entire** ``searcher.search(...)`` call — DB, embedding, reranking. What
makes that widening safe is not that the type is narrow. It is that every
raise site is **pre-IO**: a member raised after retrieval began would have a
genuine backend failure relabelled a caller 400, which is this family's own
purpose inverted.

Nothing stated or pinned that, and the base's own docstring invited the
opposite — *"subclass this rather than ``ValueError`` for any new guard over
a stated argument, and the boundaries need no edit"*. A guard added after
retrieval, or a deeper module (``arms.py``, ``embeddings.py``) subclassing
for convenience, satisfies that instruction and breaks the invariant
silently.

The asymmetry in the suite was the giveaway.
``test_search_argument_errors.py::_family()`` derives the **400-mapping**
pins from the type, so a fifth member joins those by construction; the
**pre-IO** pins were hand-written per member, one
``pool.connection.assert_not_called()`` at a time across four files, and a
fifth member joined none of them.

**Why this needs a provocation table where the 400-mapping pins do not.**
Those inject the exception into a mock searcher, so they never reach a raise
site. This property is *about* the raise sites, so each member must be
provoked for real — and a table of recipes is the listing #349 objects to,
which is why ``test_every_member_has_a_provocation`` is the reverse
cross-check that makes it safe: a member with no recipe **fails** rather than
being silently skipped. That is ``_pool_leaks.pool_constructor_calls`` and
``_harness_lock.acceptance_coverage_error``, the two places this tree already
ruled that "asks only whether a name is present" is the wrong half.

The table is keyed by raise **site**, not by type: ``KeysetCursorUnusable``
has two (the #326 walk guard and the #308 hybrid-branch guard), and the
second is far the deepest — it sits past the sort resolution and the rewrite
gate — so covering the type once would leave the site that most plausibly
drifts untested.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from localmail.config import SearchConfig
from localmail.search.argument_errors import (
    KeysetCursorUnusable,
    KeysetOrderMismatch,
    SearchArgumentRefused,
    SortNotApplicable,
    SortOrderNotApplicable,
)
from localmail.search.searcher import KeysetCursor, Searcher
from tests.test_search_argument_errors import _family


class _Embeddings:
    """Fails the test if retrieval is reached by the embedding route rather
    than the pool — ``search`` embeds before it opens a connection on some
    branches, so the pool alone is not a complete IO tripwire."""

    name = "s"
    model = "s"
    dimension = 768

    def embed_documents(self, texts):  # pragma: no cover - never reached
        raise AssertionError("no embedding may be computed")

    def embed_query(self, text):  # pragma: no cover - never reached
        raise AssertionError("no embedding may be computed")

    def health_check(self) -> None:
        pass


class _Rewriter:
    """Likewise for the smart path: an LLM round trip is IO too.

    ``name``/``model`` satisfy the ``QueryRewriter`` protocol; the Searcher
    reads them only when a rewrite is reported, which must never happen here.
    """

    name = "s"
    model = "s"

    def rewrite(self, text: str):  # pragma: no cover - never reached
        raise AssertionError("no rewrite may be attempted")


def _searcher() -> tuple[Searcher, MagicMock]:
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("no connection may be opened")
    return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embeddings(),
                    reranker=None, rewriter=_Rewriter()), pool


_TS = datetime(2026, 5, 21, tzinfo=timezone.utc)
_TEXT_CURSOR = KeysetCursor(ts=_TS, id=100, order="desc", walk="text")
_ARCHIVE_CURSOR = KeysetCursor(ts=_TS, id=100, order="desc", walk="archive")
_ASCENDING_CURSOR = KeysetCursor(ts=_TS, id=100, order="asc", walk="archive")

#: One entry per raise site: (member, what provokes it, ``search`` kwargs).
#: Keyed by site rather than by type — see the module docstring.
_PROVOCATIONS: tuple[tuple[type[SearchArgumentRefused], str, dict[str, Any]], ...] = (
    (SortNotApplicable, "rank on a textless query (#324)",
     {"query": "", "sort": "rank"}),
    (SortOrderNotApplicable, "rank with ascending order (#322)",
     {"query": "invoice", "sort": "rank", "sort_order": "asc"}),
    (KeysetOrderMismatch, "a stated order contradicting its cursor (#322)",
     {"query": "invoice", "sort_order": "desc",
      "keyset_cursor": _ASCENDING_CURSOR}),
    (KeysetCursorUnusable, "a text cursor with its query dropped (#326)",
     {"query": "", "keyset_cursor": _TEXT_CURSOR}),
    (KeysetCursorUnusable, "a keyset cursor on the hybrid branch (#308)",
     {"query": "invoice", "sort": "rank", "keyset_cursor": _ARCHIVE_CURSOR}),
)


def test_every_member_has_a_provocation() -> None:
    """The reverse cross-check that makes the table above safe.

    Without it a member added later is simply absent from the parametrised
    pin below — coverage shrinking with every test still green, which is the
    shape #347 files against the *declaration* rule and this one would
    reproduce against the *raise* rule.
    """
    provoked = {member for member, _, _ in _PROVOCATIONS}
    assert provoked == set(_family()), (
        "every SearchArgumentRefused member needs an entry in _PROVOCATIONS "
        "so its raise site is proven pre-IO"
    )


@pytest.mark.parametrize(
    "member,kwargs",
    [(m, kw) for m, _, kw in _PROVOCATIONS],
    ids=[f"{m.__name__}: {why}" for m, why, _ in _PROVOCATIONS],
)
def test_every_member_is_raised_before_any_io(
    member: type[SearchArgumentRefused], kwargs: dict[str, Any],
) -> None:
    """The contract the widened catch rests on.

    Both api boundaries wrap the whole ``searcher.search(...)`` call, so a
    member raised after retrieval began turns a backend outage into a caller
    400. Asserted on all three IO routes the Searcher can take — the pool,
    the embedding backend, and the rewriter — because a guard that moved
    below the smart rewrite would still leave ``pool.connection`` untouched.
    """
    searcher, pool = _searcher()
    with pytest.raises(member):
        searcher.search(allowed_account_ids=None, smart=True, **kwargs)
    pool.connection.assert_not_called()


def test_the_tripwires_actually_fire_when_retrieval_is_reached() -> None:
    """The positive control, and it is not optional here.

    Every assertion above is a *negative* — "this did not happen" — so a
    searcher whose pool, embeddings and rewriter were all inert would satisfy
    the whole file while proving nothing. This is the one test that shows the
    fixture can fail.
    """
    searcher, pool = _searcher()
    with pytest.raises(AssertionError, match="no connection may be opened"):
        searcher.search("invoice", allowed_account_ids=None, sort="date")
    pool.connection.assert_called()
