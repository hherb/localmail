# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The HTTP search route refuses what it cannot honour, by name (#364)."""
from __future__ import annotations

import re
from pathlib import Path

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


def _searcher_that_parses_before_anything_else():
    """A fake whose ``.search`` reproduces the one thing that matters for
    F1: the real ``Searcher.search`` parses its composed query argument
    before doing anything else, and raises the real ``parse_query``'s bare
    ``QueryParseError`` on a contradiction. The rest of the mock never runs
    that far — reaching ``AssertionError`` at all is itself the RED signal
    of "retrieval must not start", the ``_searcher()`` shape one module
    over in ``test_api_search_filter_keys.py``."""
    searcher = _fake_searcher_returning_one_hit()

    def _search(query, *args, **kwargs):
        parse_query(query)  # raises QueryParseError on a real contradiction
        raise AssertionError("retrieval must not start")

    searcher.search.side_effect = _search
    return searcher


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


_GUI_SEARCH_RS = (Path(__file__).resolve().parents[1]
                  / "gui" / "src-tauri" / "src" / "commands" / "search.rs")


def _rust_wire_names(source: str, struct: str) -> set[str]:
    """The JSON keys a serde struct in ``search.rs`` serialises.

    Strict, because a lenient reader fails silently in the direction that
    matters: a line it skips is a field it never compares. Every body line
    must be blank, a ``//`` comment, a one-line ``#[serde(...)]`` attribute
    this reader understands, or ``pub name: Type,``. Anything else — a
    private or ``pub(crate)`` field, a raw identifier, ``flatten``, a
    ``rename(serialize = …)``, a ``rename_all`` on the struct — fails here
    rather than being guessed at.
    """
    match = re.search(rf"pub struct {struct} \{{(.*?)\n\}}", source, re.S)
    assert match, f"struct {struct} not found in {_GUI_SEARCH_RS}"
    header = source[:match.start()].rsplit("\n\n", 1)[-1]
    assert "rename_all" not in header, f"{struct}: rename_all is not modelled"
    names: set[str] = set()
    rename: str | None = None
    for raw in match.group(1).splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if (attr := re.fullmatch(r"#\[serde\((.*)\)\]", line)):
            body = attr.group(1)
            assert not re.search(r"flatten|rename\s*\(|rename_all", body), (
                f"{struct}: serde attribute not modelled: {line}")
            if (r := re.search(r'\brename\s*=\s*"([^"]+)"', body)):
                rename = r.group(1)
            continue
        field = re.fullmatch(r"pub ([A-Za-z_][A-Za-z0-9_]*)\s*:.*,", line)
        assert field, f"{struct}: line not modelled: {raw.strip()}"
        names.add(rename or field.group(1))
        rename = None
    assert names, f"struct {struct}: no fields read"
    return names


def test_every_field_the_gui_sends_is_one_this_server_supports() -> None:
    """The server refuses unknown keys now (#364), so a field added to the
    GUI's Rust wire structs before the server supports it turns *every* GUI
    search into a 400 — and ``gui-ci`` mocks the server, so nothing there
    would notice. Before #364 such a field was silently ignored."""
    source = _GUI_SEARCH_RS.read_text()
    assert _rust_wire_names(source, "SearchFiltersWire") <= _SUPPORTED_FILTER_KEYS
    assert _rust_wire_names(source, "SearchRequest") <= set(SearchRequest.model_fields)


def test_the_rust_field_reader_honours_a_rename() -> None:
    """The reader's own negative control: a rename must replace the field
    name, or a renamed-to-unsupported key would pass the pin above."""
    source = ('pub struct Demo {\n'
              '    #[serde(rename = "colour")]\n'
              '    pub color: Option<String>,\n'
              '    pub query: String,\n'
              '}\n')
    assert _rust_wire_names(source, "Demo") == {"colour", "query"}


@pytest.mark.parametrize("line", [
    "    secret: Option<String>,",
    "    pub(crate) scope: Option<String>,",
    "    pub r#type: Option<String>,",
    "    #[serde(flatten)]\n    pub extra: Extra,",
    '    #[serde(rename(serialize = "x"))]\n    pub y: String,',
])
def test_the_rust_field_reader_refuses_what_it_does_not_model(line) -> None:
    """Each of these serialises a key the lenient reader skipped or
    mis-named (#366 review), so the GUI pin passed with an unsupported field
    in the struct."""
    source = f"pub struct Demo {{\n    pub query: String,\n{line}\n}}\n"
    with pytest.raises(AssertionError, match="not modelled"):
        _rust_wire_names(source, "Demo")


def test_a_comment_mentioning_rename_does_not_rename_the_next_field() -> None:
    source = ('pub struct Demo {\n'
              '    // the server may rename = "query" one day\n'
              '    pub colour: Option<String>,\n'
              '}\n')
    assert _rust_wire_names(source, "Demo") == {"colour"}


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
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["detail"].startswith(
        "filters: unknown key 'has_attachments' (did you mean 'has_attachment'?);")
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


def test_several_unknown_top_level_fields_are_all_named(
    db_dsn, api_token, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token,
                        {"query": "flight", "order": "asc", "page": 2})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == (
        "unknown fields 'order', 'page'; supported: cursor, filters, limit, "
        "query, smart, sort, sort_order"
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


@pytest.mark.parametrize("body", [
    {"filters": {"has_attachment": True}},
    # `null` is how a client whose query field is optional says "no query";
    # every sibling optional field already accepted it (#366 review).
    {"query": None, "filters": {"has_attachment": True}},
])
def test_query_may_be_omitted_or_null_for_a_filter_only_search(
    db_dsn, api_token, db_conn, api_user, body,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token, body)
    assert r.status_code == 200, r.text
    composed = parse_query(searcher.search.call_args.args[0])
    assert composed.free_text == ""
    assert composed.filters.has_attachment is True


def test_has_attachment_false_reaches_the_searcher(
    db_dsn, api_token, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    r, searcher = _post(db_dsn, api_token,
                        {"query": "hello", "filters": {"has_attachment": False}})
    assert r.status_code == 200, r.text
    composed = parse_query(searcher.search.call_args.args[0])
    assert composed.filters.has_attachment is False


def test_a_caller_granted_nothing_gets_the_400_not_an_empty_page(
    db_dsn, api_token, api_user,
) -> None:
    for filters, detail in (
        ({"has_attachments": True}, "filters: unknown key 'has_attachments'"),
        ({"date_from": "last-week"}, "date_from: expected YYYY-MM-DD"),
    ):
        r, _ = _post(db_dsn, api_token, {"query": "flight", "filters": filters})
        assert r.status_code == 400, (filters, r.text)
        assert r.json()["detail"].startswith(detail), (filters, r.text)


def test_a_has_token_in_the_query_contradicting_the_filter_is_a_400(
    db_dsn, api_token, db_conn, api_user,
) -> None:
    """The contradiction exists only in the composed string (#364 F1): a
    parse of the raw `free_text` alone never sees the filter's `has:` token,
    which is why `run_search`'s early gate parses the composition (since
    #366; #367 made that one parse supply the free text too). Unparsed
    there, the bare `QueryParseError` the real `Searcher.search` raises from
    its own parse of the composed query escaped as an unhandled 500 before
    this fix. The fake reproduces exactly that one behaviour so the RED run
    shows the real shape rather than a mock artifact.

    `raise_server_exceptions=False` is what lets the RED run of this test
    show the pre-fix 500 instead of pytest re-raising it.
    """
    _seed_acct_and_grant(db_conn, api_user.id)
    searcher = _searcher_that_parses_before_anything_else()
    client = TestClient(create_app(db_dsn=db_dsn, searcher=searcher),
                        raise_server_exceptions=False)
    r = client.post("/v1/search",
                    json={"query": "invoice has:attachment",
                          "filters": {"has_attachment": False}},
                    headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 400, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["detail"] == (
        "has: 'attachment' and 'no-attachment' contradict each other"
    )
    searcher.search.assert_not_called()
