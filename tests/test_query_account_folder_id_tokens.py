# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""DSL parser support for account_id: and folder_id: tokens.

The integer-keyed tokens populate SearchFilters.account_ids /
SearchFilters.folder_ids directly, bypassing the name-based resolution path
the Searcher uses for account: / folder: tokens. Non-integer values are
silently ignored (DSL has no escape syntax for parser errors)."""
from localmail.search.query import parse_query


def test_account_id_single_token_populates_account_ids():
    parsed = parse_query("account_id:5 alice")
    assert parsed.filters.account_ids == [5]
    assert parsed.filters.account_names == []
    assert parsed.free_text == "alice"


def test_account_id_multiple_tokens_accumulate():
    parsed = parse_query("account_id:5 account_id:7 hello")
    assert parsed.filters.account_ids == [5, 7]
    assert parsed.free_text == "hello"


def test_folder_id_single_token_populates_folder_ids():
    parsed = parse_query("folder_id:42 receipts")
    assert parsed.filters.folder_ids == [42]
    assert parsed.filters.folders is None
    assert parsed.free_text == "receipts"


def test_folder_id_multiple_tokens_accumulate():
    parsed = parse_query("folder_id:42 folder_id:99")
    assert parsed.filters.folder_ids == [42, 99]


def test_account_id_non_integer_value_treated_as_free_text():
    parsed = parse_query("account_id:foo bar")
    assert parsed.filters.account_ids is None
    assert "account_id:foo" in parsed.free_text
    assert "bar" in parsed.free_text


def test_account_id_and_account_can_coexist():
    parsed = parse_query("account_id:5 account:gmail.com")
    assert parsed.filters.account_ids == [5]
    assert parsed.filters.account_names == ["gmail.com"]
