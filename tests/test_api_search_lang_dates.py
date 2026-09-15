# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""End-to-end: the three formerly-unsupported keys round-trip through
`build_query_string` into the right DSL tokens."""
from __future__ import annotations

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import build_query_string


def test_date_from_emits_after_token():
    q = build_query_string(free_text="", filters={"date_from": "2024-01-15"})
    assert "after:2024-01-15" in q


def test_date_to_emits_before_token():
    q = build_query_string(free_text="", filters={"date_to": "2024-12-31"})
    assert "before:2024-12-31" in q


def test_lang_single_emits_lang_token():
    q = build_query_string(free_text="", filters={"lang": "en"})
    assert "lang:en" in q


def test_lang_list_emits_one_token_per_value():
    q = build_query_string(free_text="", filters={"lang": ["en", "de"]})
    assert "lang:en" in q
    assert "lang:de" in q


def test_invalid_date_from_raises():
    with pytest.raises(ValidationFailed):
        build_query_string(free_text="", filters={"date_from": "2024/01/01"})


def test_invalid_lang_raises():
    with pytest.raises(ValidationFailed):
        build_query_string(free_text="", filters={"lang": ""})
    with pytest.raises(ValidationFailed):
        build_query_string(free_text="", filters={"lang": ["en", ""]})


@pytest.mark.parametrize("lang", [
    "en has:no-attachment",
    "en folder_id:9",
    "pt BR",
    "en'",
    'e"n',
    ["en", "de fr"],
])
def test_a_lang_value_that_would_retokenize_is_refused(lang):
    """``lang`` is emitted unquoted, so whitespace or a quote in its value
    re-tokenizes the composed query: ``en has:no-attachment`` injected a
    filter the caller never set, and ``en'`` opened a quote that swallowed
    every token after it (#366 review)."""
    with pytest.raises(ValidationFailed, match="lang: expected a language code"):
        build_query_string(free_text="", filters={"lang": lang})


def test_a_lang_value_is_still_trimmed_and_lowercased():
    """Positive control: only interior whitespace and quotes are refused."""
    assert build_query_string(free_text="", filters={"lang": " EN "}) == "lang:en"


@pytest.mark.parametrize("key", ["from", "to", "subject"])
def test_a_value_that_is_empty_once_its_quotes_are_stripped_is_refused(key):
    """``_quote_value`` strips embedded quotes, so ``'"'`` became ``subject:""``,
    which parses as the free-text token ``subject:``. The filter vanished and
    a filter-only request became a text search (#366 review)."""
    with pytest.raises(ValidationFailed, match=f"{key}: value is empty"):
        build_query_string(free_text="", filters={key: '""'})
