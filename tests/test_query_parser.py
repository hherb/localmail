# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for the search query parser."""

from __future__ import annotations

from datetime import date

import pytest

from localmail.search.query import (
    ParsedQuery,
    QueryParseError,
    SearchFilters,
    parse_query,
)


def test_bare_text_query():
    q = parse_query("Berlin conference")
    assert q.free_text == "Berlin conference"
    assert q.filters == SearchFilters()


def test_from_operator():
    q = parse_query("from:anna@example.com Berlin")
    assert q.free_text == "Berlin"
    assert q.filters.from_substr == "anna@example.com"


def test_from_quoted():
    q = parse_query('from:"Anna Schmidt" Berlin')
    assert q.free_text == "Berlin"
    assert q.filters.from_substr == "Anna Schmidt"


def test_date_operators():
    q = parse_query("invoice after:2025-01-01 before:2025-12-31")
    assert q.free_text == "invoice"
    assert q.filters.after == date(2025, 1, 1)
    assert q.filters.before == date(2025, 12, 31)


def test_has_attachment_flag():
    q = parse_query("invoice has:attachment")
    assert q.filters.has_attachment is True


def test_label_account_folder():
    q = parse_query('label:work account:gmail-personal folder:"[Gmail]/Sent"')
    assert q.filters.label == "work"
    # accounts left as list[str] — searcher resolves to IDs later
    assert q.filters.account_names == ["gmail-personal"]
    assert q.filters.folders == ["[Gmail]/Sent"]


def test_multiple_same_operator_last_wins():
    q = parse_query("from:a from:b berlin")
    assert q.filters.from_substr == "b"
    assert q.free_text == "berlin"


def test_malformed_date_raises():
    with pytest.raises(QueryParseError) as exc:
        parse_query("after:not-a-date")
    assert "after" in str(exc.value).lower()


def test_has_no_attachment_flag():
    q = parse_query("invoice has:no-attachment")
    assert q.free_text == "invoice"
    assert q.filters.has_attachment is False


def test_has_values_are_case_insensitive():
    assert parse_query("has:No-Attachment").filters.has_attachment is False
    assert parse_query("has:ATTACHMENT").filters.has_attachment is True


def test_an_unknown_has_value_is_refused_not_dropped():
    """It used to vanish from the query — not a filter, and not free text."""
    with pytest.raises(
        QueryParseError,
        match="has: expected 'attachment' or 'no-attachment', got 'attachments'",
    ):
        parse_query("invoice has:attachments")


def test_contradictory_has_values_are_refused():
    with pytest.raises(
        QueryParseError,
        match="has: 'attachment' and 'no-attachment' contradict each other",
    ):
        parse_query("has:attachment has:no-attachment")


def test_a_repeated_has_value_is_accepted():
    assert parse_query("has:attachment has:attachment").filters.has_attachment is True


@pytest.mark.parametrize("query", [
    "O'Brien contract", "don't forget", "bob's invoice", "'quoted'",
])
def test_an_apostrophe_is_literal_text(query):
    """``'`` is not a quote (#373): dropping it made ``O'Brien`` the token
    ``OBrien``, which no stored tsvector holds (``'o' 'brien'``). The GUI's
    tokenizer already reads it literally; ``"`` is the only quote."""
    assert parse_query(query).free_text == query


def test_an_apostrophe_does_not_swallow_later_operators():
    q = parse_query("bob's from:alice \"final\" invoice")
    assert q.free_text == "bob's \"final\" invoice"
    assert q.filters.from_substr == "alice"


def test_an_operator_value_keeps_its_apostrophe():
    assert parse_query("from:o'brien").filters.from_substr == "o'brien"


def test_an_apostrophe_inside_a_phrase_is_kept():
    """Inside ``"…"`` the ``'`` reaches both the free text (the phrase
    ``websearch_to_tsquery`` reads) and the operator value: dropping it from
    either is #373 again, for a phrase."""
    q = parse_query('"O\'Brien contract"')
    assert q.free_text == '"O\'Brien contract"'
    assert q.semantic_text == "O'Brien contract"
    assert parse_query('subject:"don\'t forget"').filters.subject_substr == "don't forget"


def test_a_single_quoted_operator_value_splits_on_whitespace():
    """The documented cost of #373: ``'`` no longer groups a value; write ``"``."""
    q = parse_query("from:'Anna Schmidt'")
    assert q.filters.from_substr == "'Anna"
    assert q.free_text == "Schmidt'"


def test_a_single_quoted_has_value_is_refused():
    """``has:'attachment`` is an unknown ``has:`` value since #373, not a quote."""
    with pytest.raises(QueryParseError, match="has:"):
        parse_query("has:'attachment")
