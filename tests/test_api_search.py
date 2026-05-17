from datetime import date
from unittest.mock import MagicMock

import pytest

from localmail.api.search import build_query_string, run_search
from localmail.api.errors import ValidationFailed


def test_build_query_string_includes_dsl_for_filters() -> None:
    q = build_query_string(
        free_text="invoice",
        filters={"from": "anna@", "after": "2024-01-01", "has_attachment": True},
    )
    assert "invoice" in q
    assert 'from:"anna@"' in q
    assert "after:2024-01-01" in q
    assert "has:attachment" in q


def test_build_query_string_validates_date_format() -> None:
    with pytest.raises(ValidationFailed):
        build_query_string(free_text="x", filters={"after": "not-a-date"})


def test_filter_value_with_dsl_injection_is_quoted() -> None:
    """Regression: a filter value containing `account:other` must not break
    out into an additional operator. Quoting forces the parser to treat the
    whole value as one token."""
    from localmail.search.query import parse_query

    q = build_query_string(
        free_text="hello",
        filters={"from": "alice OR account:other"},
    )
    parsed = parse_query(q)
    assert parsed.filters.from_substr == "alice OR account:other"
    assert parsed.filters.account_names == []


def test_filter_value_with_embedded_quote_is_stripped() -> None:
    """The DSL tokenizer has no escape syntax. We strip embedded quotes
    rather than try to escape them — substring filters don't need them."""
    q = build_query_string(
        free_text="x",
        filters={"subject": 'has "quotes" in it'},
    )
    assert 'subject:"has quotes in it"' in q


@pytest.mark.parametrize("unsupported_key, value", [
    ("account_ids", ["1", "3"]),
    ("folder_ids", ["10"]),
    ("date_from", "2024-01-01"),
    ("date_to", "2024-12-31"),
    ("lang", "en"),
])
def test_unsupported_filter_keys_raise_validation(unsupported_key, value) -> None:
    """Filters in the API schema but not wired to the searcher must 400.

    Without this, a client requesting `account_ids: ["1"]` gets cross-account
    results back, silently — the worst possible outcome for a search API.
    """
    with pytest.raises(ValidationFailed):
        build_query_string(free_text="x", filters={unsupported_key: value})


def test_empty_unsupported_filter_values_do_not_raise() -> None:
    """Pydantic's exclude_none can still leave empty lists / empty strings;
    those are equivalent to "filter not set" and must not 400."""
    build_query_string(free_text="x", filters={"account_ids": []})
    build_query_string(free_text="x", filters={"date_from": ""})
    build_query_string(free_text="x", filters={"lang": None})


def test_run_search_calls_searcher_and_maps_results() -> None:
    fake_searcher = MagicMock()
    fake_result = MagicMock()
    fake_result.message_id = 42
    fake_result.account_id = 1
    fake_result.rank = 1
    fake_result.score = 0.91
    fake_result.rrf_score = 0.5
    fake_result.subject = "Re: kid"
    fake_result.from_addr = "anna@x"
    fake_result.from_name = "Anna"
    fake_result.date_sent = None
    fake_result.snippet = "…bus leaves…"
    fake_result.snippet_source = "body"
    fake_result.attachment_filename = None
    fake_result.matched_chunk_id = None
    fake_result.matched_chunk_table = "message_chunks"

    fake_page = MagicMock()
    fake_page.results = [fake_result]
    fake_page.search_token = "tok-1"
    fake_page.timing_ms = {"total": 12.5}

    fake_searcher.search.return_value = fake_page

    out = run_search(
        searcher=fake_searcher,
        free_text="bus",
        filters={},
        limit=20,
        cursor=None,
    )

    fake_searcher.search.assert_called_once()
    assert len(out["results"]) == 1
    r = out["results"][0]
    assert r["message_id"] == "42"
    assert r["subject"] == "Re: kid"
    assert r["score"] == 0.91
    assert r["matched_arms"]  # non-empty
    assert out["took_ms"] == 12.5
    assert out["next_cursor"] == "tok-1"
