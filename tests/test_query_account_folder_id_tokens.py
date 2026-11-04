# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""DSL parser support for account_id: and folder_id: tokens.

The integer-keyed tokens populate SearchFilters.account_ids /
SearchFilters.folder_ids directly, bypassing the name-based resolution path
the Searcher uses for account: / folder: tokens. A non-integer value is
refused (#378)."""
import pytest

from localmail.search.query import QueryParseError, parse_query


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


def test_account_id_non_integer_value_is_refused():
    """It used to fall back to free text (#378); the refusal's own pins are
    in test_query_operator_values.py."""
    with pytest.raises(QueryParseError, match="account_id: expected an integer id"):
        parse_query("account_id:foo bar")


def test_account_id_and_account_can_coexist():
    parsed = parse_query("account_id:5 account:gmail.com")
    assert parsed.filters.account_ids == [5]
    assert parsed.filters.account_names == ["gmail.com"]
