# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The HTTP search route refuses what it cannot honour, by name (#364)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from localmail.api.search import _SUPPORTED_FILTER_KEYS
from localmail.search.query import parse_query
from localmail.serve.app import create_app
from localmail.serve.routes.search import SearchFiltersModel, SearchRequest
from tests.test_serve_search_route import (
    _fake_searcher_returning_one_hit,
    _seed_acct_and_grant,
)


def _post(db_dsn: str, api_token: str, body: dict):
    searcher = _fake_searcher_returning_one_hit()
    client = TestClient(create_app(db_dsn=db_dsn, searcher=searcher))
    response = client.post("/v1/search", json=body,
                           headers={"Authorization": f"Bearer {api_token}"})
    return response, searcher


def test_the_filter_model_accepts_exactly_the_supported_keys() -> None:
    """A field the model accepts but the composer does not know would be
    dropped downstream — the gap `_KNOWN_UNSUPPORTED_FILTER_KEYS` stood for."""
    wire = {f.alias or name for name, f in SearchFiltersModel.model_fields.items()}
    assert wire == _SUPPORTED_FILTER_KEYS


def test_the_supported_field_list_is_the_request_model() -> None:
    assert sorted(SearchRequest.model_fields) == [
        "cursor", "filters", "limit", "query", "smart", "sort", "sort_order",
    ]


def test_an_unknown_filter_key_is_a_400_naming_it(
    db_dsn, api_token, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token,
                        {"query": "flight", "filters": {"has_attachments": True}})
    assert r.status_code == 400, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["detail"] == (
        "filters: unknown key 'has_attachments' (did you mean 'has_attachment'?); "
        "supported: account_ids, after, before, date_from, date_to, folder_ids, "
        "from, has_attachment, lang, subject, to"
    )
    searcher.search.assert_not_called()


def test_a_null_valued_unknown_filter_key_is_still_a_400(
    db_dsn, api_token, db_conn, api_user,
) -> None:
    """``exclude_none`` would drop it before ``run_search`` saw it; the route
    must forward unknown keys with their nulls."""
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token,
                        {"query": "flight", "filters": {"has_attachments": None}})
    assert r.status_code == 400, r.text
    searcher.search.assert_not_called()


def test_an_unknown_top_level_field_is_a_400_naming_it(
    db_dsn, api_token, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token, {"query": "flight", "order": "asc"})
    assert r.status_code == 400, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["detail"] == (
        "unknown field 'order'; supported: cursor, filters, limit, query, smart, "
        "sort, sort_order"
    )
    searcher.search.assert_not_called()


@pytest.mark.parametrize("body", [
    # The desktop GUI's shape (gui/src-tauri/src/commands/search.rs).
    {"query": "hello", "filters": {"account_ids": ["1"], "has_attachment": True},
     "limit": 20, "sort": "date"},
    # Kastellan's mail worker, as captured in #364.
    {"query": "flight", "filters": {"has_attachment": True, "account_ids": ["1"]},
     "limit": 10},
])
def test_real_client_shapes_still_succeed(
    db_dsn, api_token, db_conn, api_user, body,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    r, _ = _post(db_dsn, api_token, body)
    assert r.status_code == 200, r.text


def test_query_may_be_omitted_for_a_filter_only_search(
    db_dsn, api_token, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token, {"filters": {"has_attachment": True}})
    assert r.status_code == 200, r.text
    composed = parse_query(searcher.search.call_args.args[0])
    assert composed.free_text == ""
    assert composed.filters.has_attachment is True


def test_a_caller_granted_nothing_gets_the_400_not_an_empty_page(
    db_dsn, api_token, api_user,
) -> None:
    for filters in ({"has_attachments": True}, {"date_from": "last-week"}):
        r, _ = _post(db_dsn, api_token, {"query": "flight", "filters": filters})
        assert r.status_code == 400, (filters, r.text)
