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
