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
