# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""End-to-end: the three formerly-unsupported keys round-trip through
`build_query_string` into the right DSL tokens."""
from __future__ import annotations

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import build_query_string
from localmail.search.query import parse_query


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


@pytest.mark.parametrize("key", ["after", "before", "date_from", "date_to"])
@pytest.mark.parametrize("value", [
    "2020-01- 1",   # strptime's %d admits a space, which splits the token
    " 2020-01-01",  # refused by strptime too; pinned so the shape stays a fullmatch
    "٢٠٢٠-01-01",   # Arabic-Indic year: strptime's %Y is Unicode-aware
])
def test_a_date_that_is_not_one_ascii_token_is_refused(key, value):
    """A structured date is emitted unquoted ahead of the free text, so it
    must be one token (#367). ``strptime`` alone accepted the first and the
    last, and ``2020-01- 1`` became ``after:2020-01-`` plus a stray ``1``: a
    400 naming the wrong key with a truncated value."""
    with pytest.raises(ValidationFailed, match=f"{key}: expected YYYY-MM-DD"):
        build_query_string(free_text="", filters={key: value})


def test_a_date_without_zero_padding_is_still_accepted():
    """The query parser accepts ``after:2020-1-1``, and the desktop client
    moves a typed date into a structured filter when it contradicts a chip,
    so the structured rule must not be stricter than the typed one: that
    would turn a search the query accepted into a 400 on Enter."""
    assert build_query_string(free_text="", filters={"date_from": "2020-1-1"}) == (
        "after:2020-1-1")
    assert parse_query("after:2020-1-1").filters.after is not None


def test_a_well_formed_date_that_does_not_exist_is_still_refused():
    """The shape check does not replace the calendar check."""
    with pytest.raises(ValidationFailed, match="date_from: expected YYYY-MM-DD"):
        build_query_string(free_text="", filters={"date_from": "2020-02-30"})


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
