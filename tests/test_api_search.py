# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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


@pytest.mark.parametrize("free_text", [
    "invoice",
    "",
    "   ",
    "subject:invoice",
    'quoted "phrase" here',
    "multi word text",
    "trailing ",
])
@pytest.mark.parametrize("filters", [
    {},
    {"account_ids": [1, 2]},
    {"folder_ids": [7]},
    {"from": "alice@example.com"},
    {"from": "alice OR account:other"},
    {"subject": "  "},
    {"after": "2026-01-01", "before": "2026-02-01"},
    {"has_attachment": True},
    {"lang": "en"},
    {"to": 'bob "the" builder'},
])
def test_build_query_string_is_free_text_neutral(
    free_text: str, filters: dict,
) -> None:
    """Composing filters in must not change what counts as the free text.

    #326's two guards both apply ``parse_query`` and both read
    ``.free_text``, but not to the same string: ``api.run_search``'s gate
    parses the **raw** request field, while ``Searcher.search`` parses
    ``build_query_string(free_text, scoped_filters)`` — the composed query,
    which ``_scope_filters_by_acl`` has already appended ``account_id:``
    tokens to. They agree only because this composer is free-text-neutral,
    which is a property of the composer and of neither guard.

    Unpinned, that is a third reading of one rule in a cluster whose own
    history is two predicates disagreeing about what counts as a blank
    query (#308's follow-up). CLAUDE.md asserted the equivalence for #308;
    this is what makes it true rather than written down.
    """
    from localmail.search.query import parse_query

    composed = parse_query(build_query_string(free_text=free_text,
                                              filters=filters)).free_text
    assert composed.strip() == parse_query(free_text).free_text.strip()


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


def test_known_unsupported_filter_keys_is_empty() -> None:
    """Every v1 spec filter key is wired through to the Searcher."""
    from localmail.api.search import _KNOWN_UNSUPPORTED_FILTER_KEYS, _SUPPORTED_FILTER_KEYS
    assert _KNOWN_UNSUPPORTED_FILTER_KEYS == frozenset()
    assert {"date_from", "date_to", "lang"} <= _SUPPORTED_FILTER_KEYS


@pytest.mark.parametrize("key, value, expected_token", [
    ("date_from", "2024-01-01", "after:2024-01-01"),
    ("date_to", "2024-12-31", "before:2024-12-31"),
    ("lang", "en", "lang:en"),
])
def test_formerly_unsupported_keys_now_emit_tokens(key, value, expected_token) -> None:
    """date_from/date_to/lang were previously rejected; Sub-plan 5 wires them through."""
    q = build_query_string(free_text="x", filters={key: value})
    assert expected_token in q


def test_empty_unsupported_filter_values_do_not_raise() -> None:
    """Pydantic's exclude_none can still leave empty lists / empty strings;
    those are equivalent to "filter not set" and must not 400."""
    build_query_string(free_text="x", filters={"account_ids": []})
    build_query_string(free_text="x", filters={"date_from": ""})
    build_query_string(free_text="x", filters={"lang": None})


def test_build_query_string_emits_account_id_tokens():
    out = build_query_string(
        free_text="hello",
        filters={"account_ids": ["5", "7"]},
    )
    assert "account_id:5" in out
    assert "account_id:7" in out
    assert out.startswith("hello")


def test_build_query_string_emits_folder_id_tokens():
    out = build_query_string(
        free_text="invoices",
        filters={"folder_ids": ["42"]},
    )
    assert "folder_id:42" in out
    assert out.startswith("invoices")


def test_build_query_string_account_ids_handles_int_or_str():
    """The API layer accepts both — Pydantic models may coerce to str."""
    out = build_query_string(free_text="", filters={"account_ids": [5, "7"]})
    assert "account_id:5" in out
    assert "account_id:7" in out


def test_build_query_string_empty_account_ids_is_no_op():
    out = build_query_string(free_text="hello", filters={"account_ids": []})
    assert out == "hello"


def test_build_query_string_malformed_account_id_raises():
    with pytest.raises(ValidationFailed):
        build_query_string(free_text="", filters={"account_ids": ["foo"]})


def test_wire_date_reflects_internal_date_when_set() -> None:
    """The wire `date` field is the column the sort key actually uses —
    ``COALESCE(internal_date, date_sent)``. Returning only the header
    ``Date:`` value while sorting by INTERNALDATE makes the displayed
    dates look out of order whenever the two differ (forwarded mail,
    mailing-list delays, sender clock skew, mid-rollout backfill).
    """
    from datetime import datetime, timezone

    from localmail.api.search import _to_api_result
    from localmail.search.searcher import SearchResult

    header_date = datetime(2022, 1, 1, tzinfo=timezone.utc)
    arrived = datetime(2026, 5, 20, tzinfo=timezone.utc)
    r = SearchResult(
        message_id=1, account_id=1, rank=1, score=0.5, rrf_score=0.5,
        subject="s", from_addr="a@b", from_name="A",
        date_sent=header_date, internal_date=arrived,
        snippet="", snippet_source="body",
        attachment_filename=None, matched_chunk_id=None,
        matched_chunk_table="message_chunks",
    )
    out = _to_api_result(r)
    assert out["date"] == arrived.isoformat()


def test_wire_date_falls_back_to_date_sent_when_internal_date_null() -> None:
    """Legacy / un-backfilled rows still surface a date; the sort key
    falls back to ``date_sent`` so the wire field must do the same."""
    from datetime import datetime, timezone

    from localmail.api.search import _to_api_result
    from localmail.search.searcher import SearchResult

    header_date = datetime(2022, 1, 1, tzinfo=timezone.utc)
    r = SearchResult(
        message_id=1, account_id=1, rank=1, score=0.5, rrf_score=0.5,
        subject="s", from_addr="a@b", from_name="A",
        date_sent=header_date, internal_date=None,
        snippet="", snippet_source="body",
        attachment_filename=None, matched_chunk_id=None,
        matched_chunk_table="message_chunks",
    )
    out = _to_api_result(r)
    assert out["date"] == header_date.isoformat()


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
    fake_page.has_more_in_pool = False
    fake_page.can_grow_pool = False
    fake_page.candidates_per_arm = 50
    fake_page.page = 1
    # Pool-cursor mock — explicit None keeps `_next_cursor` out of the
    # keyset branch (MagicMock's auto-attr would be truthy).
    fake_page.next_keyset = None
    # Set explicitly for the reason the route-level fakes are (#345): an
    # unset MagicMock attribute is a value, not an error. This assertion is
    # api-level so nothing encodes it here, but leaving it auto-mocked is
    # how the wire-level instance of the same fake went unnoticed.
    fake_page.sort_applied = "rank"

    fake_searcher.search.return_value = fake_page

    out = run_search(
        searcher=fake_searcher,
        free_text="bus",
        filters={},
        limit=20,
        allowed_account_ids=[1],
        user_id=99,
    )

    fake_searcher.search.assert_called_once()
    assert len(out["results"]) == 1
    r = out["results"][0]
    assert r["message_id"] == "42"
    assert r["subject"] == "Re: kid"
    assert r["score"] == 0.91
    assert r["matched_arms"]  # non-empty
    assert out["took_ms"] == 12.5
    assert out["next_cursor"] is None


def test_run_search_forwards_allowed_account_ids_to_searcher() -> None:
    """The ACL clamp is enforced Searcher-side; run_search must hand it the
    full grant so a smuggled `account_id:` free-text token can't widen scope."""
    s = _fake_searcher_for_smart(smart_available=True, page_status="not_requested")
    run_search(searcher=s, free_text="invoice account_id:7", filters={},
               limit=20, allowed_account_ids=[5], user_id=9)
    assert s.search.call_args.kwargs["allowed_account_ids"] == [5]


def _fake_searcher_for_smart(
    *, smart_available: bool, page_status: str = "not_requested",
    page_note=None, page_note_code=None,
):
    s = MagicMock()
    s.smart_available = smart_available
    page = MagicMock()
    page.results = []
    page.search_token = "tok-1"
    page.timing_ms = {"total": 1.0}
    page.has_more_in_pool = False
    page.can_grow_pool = False
    page.candidates_per_arm = 50
    page.page = 1
    page.next_keyset = None
    page.sort_applied = "rank"
    page.rewrite_status = page_status
    page.rewrite_note = page_note
    page.rewrite_note_code = page_note_code
    s.search.return_value = page
    return s


def test_run_search_forwards_smart_when_available():
    s = _fake_searcher_for_smart(smart_available=True, page_status="applied")
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9, smart=True)
    assert s.search.call_args.kwargs["smart"] is True
    assert out["rewrite_status"] == "applied"
    assert out["rewrite_note"] is None
    assert out["rewrite_skipped"] is False
    assert out["rewrite_note_code"] is None


def test_run_search_smart_surfaces_page_failure():
    s = _fake_searcher_for_smart(
        smart_available=True, page_status="failed",
        page_note="could not reach the rewriter service",
        page_note_code="unreachable",
    )
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9, smart=True)
    assert out["rewrite_status"] == "failed"
    assert out["rewrite_note"] == "could not reach the rewriter service"
    assert out["rewrite_skipped"] is True
    assert out["rewrite_note_code"] == "unreachable"


def test_run_search_smart_without_rewriter_degrades_gracefully():
    """smart=True on a server with no rewriter: do NOT raise; run un-rewritten,
    report status=unavailable (rewrite_skipped=True), and still return the dict."""
    s = _fake_searcher_for_smart(smart_available=False)
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9, smart=True)
    # effective_smart must be False so the searcher's RuntimeError guard never fires
    assert s.search.call_args.kwargs["smart"] is False
    assert out["rewrite_status"] == "unavailable"
    assert out["rewrite_note"] == "smart search is not configured on this server"
    assert out["rewrite_skipped"] is True
    assert out["rewrite_note_code"] == "not_configured"
    assert "results" in out


def test_run_search_default_smart_is_false():
    s = _fake_searcher_for_smart(smart_available=True, page_status="not_requested")
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9)
    assert s.search.call_args.kwargs["smart"] is False
    assert out["rewrite_status"] == "not_requested"
    assert out["rewrite_skipped"] is False
    assert out["rewrite_note_code"] is None


def test_run_search_empty_acl_short_circuit_includes_rewrite_status():
    """The ACL short-circuit (no grants) keeps the stable wire shape."""
    s = MagicMock()
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[], user_id=9, smart=True)
    assert out == {"results": [], "next_cursor": None, "total_estimate": None,
                   "took_ms": 0.0, "rewrite_skipped": False,
                   # Present here rather than omitted (#345): this branch has
                   # no cursor to infer an ordering from, and its empty page
                   # is byte-identical to "you have reached the end".
                   "sort_applied": "rank",
                   # And `rankable` beside it (#353) — exact on every mode
                   # here, being a property of the query alone.
                   "rankable": True,
                   "rewrite_status": "not_requested", "rewrite_note": None,
                   "rewrite_note_code": None}
    s.search.assert_not_called()


def test_run_search_smart_on_continuation_cursor_reports_not_attempted():
    """smart is a page-1 signal: a pool-cursor continuation must NOT re-rewrite
    and reports not_attempted (rewrite_skipped stays False) even when the
    caller re-sends smart=True."""
    from localmail.api.search_cursor import SearchCursor, encode_search_cursor

    s = _fake_searcher_for_smart(smart_available=True, page_status="applied")
    # Continuation goes through searcher.continue_page, not searcher.search.
    s.continue_page.return_value = s.search.return_value
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=2))
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9, smart=True, cursor=cursor)
    s.search.assert_not_called()
    s.continue_page.assert_called_once()
    assert out["rewrite_status"] == "not_attempted"
    assert out["rewrite_note"] == (
        "smart query rewriting applies to the first page only; "
        "this is a continuation page"
    )
    assert out["rewrite_skipped"] is False
    assert out["rewrite_note_code"] == "continuation_page"
