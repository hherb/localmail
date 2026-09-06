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

from typing import get_args

import pytest

from localmail.search.sort_axes import (
    DEFAULT_SORT,
    DEFAULT_SORT_ORDER,
    TEXTLESS_SORT,
    SortMode,
    SortOrder,
    is_rankable,
    resolve_sort,
    sort_applicability_error,
    sort_membership_error,
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
    a second ``"date"`` literal fails here rather than silently later.

    **One** non-local thing rests on it, and it used to be two. What remains:
    page 1 accepts ``sort=TEXTLESS_SORT`` for a textless query and mints a
    keyset cursor; page 2's ``_reject_sort_mismatch`` compares the stated sort
    against ``KEYSET_SORT``. A divergence is #324's own shape — accepted on
    page 1, refused on page 2.

    What has gone: ``run_search``'s keyset branch *used to* omit
    ``SortNotApplicable`` from its catch, safe only because
    ``sort_applicability_error`` returns ``None`` for ``TEXTLESS_SORT``. Since
    #344 both branches catch the ``SearchArgumentRefused`` family rather than
    naming members, so that omission is not expressible and the divergence
    would now be a 400 rather than a 500. The assertion below is unchanged and
    still earns its keep on the first property alone.
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


# --- the vocabulary rule (#348, pinned by its review) ----------------------

def test_a_well_formed_pair_is_accepted() -> None:
    """The positive control. A rule that refused everything would satisfy
    every negative below, and the ``get_args`` derivation is what makes this
    hold for a member added to either ``Literal`` tomorrow."""
    for sort in (None, *get_args(SortMode)):
        for order in (None, *get_args(SortOrder)):
            assert sort_membership_error(sort=sort, sort_order=order) is None


def test_the_vocabulary_is_derived_from_the_literals_not_restated() -> None:
    """Freezing either list to a hard-coded tuple survived the whole suite
    when this function shipped, because nothing here imported it — a future
    ``SortMode`` member would then be silently refused, which is the failure
    ``date_keyset.DATE_ORDER_BY_SQL``'s import-time completeness check exists
    to prevent one module over.

    Asserted against ``get_args`` rather than against ``["asc", "desc"]``, so
    widening a ``Literal`` does not need this test edited — the property is
    "the rule ranges over the type", not "the rule allows these two".
    """
    for member in get_args(SortMode):
        assert sort_membership_error(sort=member, sort_order=None) is None
        assert sort_membership_error(sort=member.upper() + "_",
                                     sort_order=None) is not None
    for member in get_args(SortOrder):
        assert sort_membership_error(sort=None, sort_order=member) is None
        assert sort_membership_error(sort=None,
                                     sort_order=member.upper() + "_") is not None


@pytest.mark.parametrize("sort", ["Date", "DATE", "dat", "relevance", ""])
def test_an_unknown_sort_is_named_with_its_vocabulary(sort: str) -> None:
    """The message quotes the offending value and lists the alternatives —
    it is the whole remedy a caller gets, at both layers."""
    problem = sort_membership_error(sort=sort, sort_order=None)
    assert problem is not None
    assert repr(sort) in problem
    assert "date" in problem and "rank" in problem


@pytest.mark.parametrize("order", ["ASC", "ascending", ""])
def test_an_unknown_sort_order_is_named_with_its_vocabulary(order: str) -> None:
    problem = sort_membership_error(sort=None, sort_order=order)
    assert problem is not None
    assert repr(order) in problem
    assert "asc" in problem and "desc" in problem


def test_an_unstated_axis_is_not_a_claim_that_can_be_wrong() -> None:
    """``None`` means "nothing to check", which is what lets ``Searcher.search``
    read both axes as *stated* and so decide them above the cursor block that
    resolves them."""
    assert sort_membership_error(sort=None, sort_order=None) is None
    assert sort_membership_error(sort=None, sort_order="asc") is None
    assert sort_membership_error(sort="date", sort_order=None) is None


def test_sort_is_reported_before_sort_order_when_both_are_wrong() -> None:
    """One message, not two, and ``sort`` wins — so a caller who misspelled
    both is told about ``sort`` first and meets the second on their retry.

    Pinned because it is unspecified otherwise, and because the alternative
    (reporting both) is a real option someone may reach for: this records
    that the sequential shape is the decision, not an accident.
    """
    problem = sort_membership_error(sort="Date", sort_order="ASC")
    assert problem is not None
    assert "unknown sort 'Date'" in problem
    assert "sort_order" not in problem


# ---------------------------------------------------------------------------
# is_rankable — the question the sort selector needs answered (#353)
# ---------------------------------------------------------------------------
#
# `sort_applied` (#345) reports which ordering ran, which is exact only when
# the caller stated nothing: a stated `date` is honoured for *any* query, so
# `sort_applied == "date"` cannot distinguish "there was nothing to rank"
# from "rank was available and not chosen". The GUI inferred the first from
# the second and paid for it in #353 — a click that re-enabled Relevance on
# a query that genuinely could not be ranked.


@pytest.mark.parametrize("free_text", WITH_TEXT)
def test_a_query_with_free_text_is_rankable(free_text: str) -> None:
    assert is_rankable(free_text=free_text) is True


@pytest.mark.parametrize("free_text", TEXTLESS)
def test_a_query_with_no_free_text_is_not_rankable(free_text: str) -> None:
    assert is_rankable(free_text=free_text) is False


@pytest.mark.parametrize("free_text", TEXTLESS + WITH_TEXT)
@pytest.mark.parametrize("requested", [None, "rank", "date"])
def test_resolve_sort_returns_rank_exactly_when_the_query_is_rankable(
    free_text: str, requested: SortMode | None,
) -> None:
    """The one-authority pin, and the reason this rule lives here.

    ``resolve_sort`` asks ``is_rankable`` rather than repeating the
    classification, so a response can never carry ``rankable=False``
    alongside ``sort_applied="rank"``. Stated over every request shape
    because the implication only runs one way: a rankable query resolves to
    ``date`` whenever ``date`` was asked for.
    """
    resolved = resolve_sort(requested=requested, free_text=free_text)
    if resolved == "rank":
        assert is_rankable(free_text=free_text) is True
    if not is_rankable(free_text=free_text):
        assert resolved == TEXTLESS_SORT


@pytest.mark.parametrize("free_text", TEXTLESS + WITH_TEXT)
def test_rankability_is_exactly_what_the_applicability_rule_judges(
    free_text: str,
) -> None:
    """``sort_applicability_error`` refuses a stated ``rank`` on precisely
    the queries this reports unrankable — the two readings cannot drift."""
    refused = sort_applicability_error(
        requested="rank", free_text=free_text,
    ) is not None
    assert refused is not is_rankable(free_text=free_text)
