# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""DSL parser tests for the `lang:` token.

`lang:en` populates SearchFilters.languages as a list; multiple tokens append.
The value is lowercased and stripped — case-insensitive matching downstream.
"""
from __future__ import annotations

import pytest

from localmail.search.query import parse_query, QueryParseError


def test_single_lang_token():
    parsed = parse_query("invoice lang:en")
    assert parsed.free_text == "invoice"
    assert parsed.filters.languages == ["en"]


def test_multiple_lang_tokens_accumulate():
    parsed = parse_query("lang:de lang:en")
    assert parsed.free_text == ""
    assert parsed.filters.languages == ["de", "en"]


def test_lang_token_is_lowercased():
    parsed = parse_query("lang:EN")
    assert parsed.filters.languages == ["en"]


def test_lang_token_empty_value_raises():
    with pytest.raises(QueryParseError):
        parse_query("lang:")


def test_lang_token_strips_whitespace_in_quoted_value():
    parsed = parse_query('lang:" en "')
    assert parsed.filters.languages == ["en"]
