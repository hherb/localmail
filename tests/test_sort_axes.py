# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A query with no free text can only be served date-ordered (#324).

The hybrid pool degenerates for a query with nothing to rank against — the
lexical arms early-return with no terms and the vector arms rank by distance
to the embedding of the empty string — so ``_date_keyset_search`` has always
answered such a query, whatever ``sort`` said. That drop was silent, which
#322 turned from a cosmetic wart into a contradiction: the walk now mints a
cursor recording ``date``, so a client echoing its own ``sort="rank"`` back
was accepted on page 1 and refused on page 2.

The rule these tests pin resolves it at page 1 instead, which is where this
cluster's own rule (#308, #312) says a stated parameter the server will not
honour belongs: reported, never dropped.

Two halves, deliberately co-located — ``resolve_sort`` says what will run and
``sort_applicability_error`` says whether the caller was told. Split, they
are two predicates for one question, which is the shape of the #308
follow-up defect.
"""
from __future__ import annotations

import pytest

from localmail.search.sort_axes import (
    DEFAULT_SORT,
    DEFAULT_SORT_ORDER,
    TEXTLESS_SORT,
    SortMode,
    resolve_sort,
    sort_applicability_error,
)

#: Queries whose ``parse_query(...).free_text`` is empty. The callers pass
#: the *parsed* free text, so operator-only queries arrive here as ``""`` —
#: they are listed as their own case only in the api/Searcher tests, where
#: the parse is real.
TEXTLESS = ("", "   ", "\t\n")

WITH_TEXT = ("invoice", "  invoice  ", "a")


def test_the_defaults_are_unchanged() -> None:
    """The positive control for every claim below: nothing about a query
    that *does* have free text moved."""
    assert DEFAULT_SORT == "rank"
    assert DEFAULT_SORT_ORDER == "desc"


def test_the_textless_sort_is_date() -> None:
    """Named once rather than spelled at each of the four read sites."""
    assert TEXTLESS_SORT == "date"


def test_the_keyset_cursor_s_sort_is_the_textless_one() -> None:
    """One fact seen from two ends, so it is asserted and not just aliased.

    ``KEYSET_SORT`` is now ``TEXTLESS_SORT`` by construction, which is the
    real protection; this pins the *property* so that un-aliasing it back to
    a second ``"date"`` literal fails here rather than silently later. Two
    things rest on it, neither of them local:

    1. Page 1 accepts ``sort=TEXTLESS_SORT`` for a textless query and mints a
       keyset cursor; page 2's ``_reject_sort_mismatch`` compares the stated
       sort against ``KEYSET_SORT``. A divergence is #324's own shape —
       accepted on page 1, refused on page 2.
    2. ``run_search``'s keyset branch omits ``SortNotApplicable`` from its
       catch, which is safe only because ``sort_applicability_error`` returns
       ``None`` for ``TEXTLESS_SORT``. A divergence turns every keyset
       continuation of a blank-query walk into a 500.
    """
    from localmail.api.search_cursor import KEYSET_SORT

    assert KEYSET_SORT == TEXTLESS_SORT
    assert sort_applicability_error(requested=KEYSET_SORT, free_text="") is None


@pytest.mark.parametrize("free_text", WITH_TEXT)
@pytest.mark.parametrize("requested", [None, "rank", "date"])
def test_a_query_with_free_text_keeps_the_stated_sort(
    requested: SortMode | None, free_text: str,
) -> None:
    expected = DEFAULT_SORT if requested is None else requested
    assert resolve_sort(requested=requested, free_text=free_text) == expected
    assert sort_applicability_error(requested=requested,
                                    free_text=free_text) is None


@pytest.mark.parametrize("free_text", TEXTLESS)
def test_an_unstated_sort_on_a_textless_query_resolves_to_date(
    free_text: str,
) -> None:
    """The inverse face of the refusal, and the half that keeps clients
    working: omitting ``sort`` is the documented way to page, so it must
    resolve to the branch that will actually serve the request rather than
    to ``DEFAULT_SORT``. Without this the rank+asc guard downstream reads
    ``rank`` and refuses ``sort_order='asc'`` for a path it would never
    have taken."""
    assert resolve_sort(requested=None, free_text=free_text) == "date"
    assert sort_applicability_error(requested=None,
                                    free_text=free_text) is None


@pytest.mark.parametrize("free_text", TEXTLESS)
def test_a_stated_date_on_a_textless_query_is_honoured(
    free_text: str,
) -> None:
    assert resolve_sort(requested="date", free_text=free_text) == "date"
    assert sort_applicability_error(requested="date",
                                    free_text=free_text) is None


@pytest.mark.parametrize("free_text", TEXTLESS)
def test_a_stated_rank_on_a_textless_query_is_refused(free_text: str) -> None:
    message = sort_applicability_error(requested="rank", free_text=free_text)
    assert message is not None


@pytest.mark.parametrize("free_text", TEXTLESS)
def test_the_refusal_names_the_remedy_and_the_reason(free_text: str) -> None:
    """A refusal whose message does not say what to send instead is a wall.

    Both remedies are named because they are different requests, not two
    spellings of one: ``sort='date'`` is a statement that survives being
    echoed back with the cursor, while omitting it is what the paging
    contract asks for.
    """
    message = sort_applicability_error(requested="rank", free_text=free_text)
    assert message is not None
    assert "sort='date'" in message
    assert "omit sort" in message
    assert "free text" in message


def test_resolve_sort_reports_what_will_run_even_for_a_refused_request() -> None:
    """``resolve_sort`` answers "what will serve this", not "what is
    allowed" — the two rules are read in order, and it is the caller that
    decides a refusal is an error.

    Pinned because the tempting shortcut is to have ``resolve_sort`` raise,
    which would give the api boundary and the Searcher two different
    exception types for one rule and put the wording in two places.
    """
    assert resolve_sort(requested="rank", free_text="") == TEXTLESS_SORT
