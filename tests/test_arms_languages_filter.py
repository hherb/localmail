# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""_filter_sql languages predicate — only messages whose primary language
matches the supplied list are kept.

Per Phase 1 + 2 design, `messages.body_lang` (text, nullable, migration 0015)
is the column populated by per-message language detection. The predicate is
`m.body_lang = ANY(%s)`. Messages with NULL body_lang are excluded when a
filter is set — opt-in semantics.
"""
from __future__ import annotations

from localmail.search.arms import _filter_sql
from localmail.search.query import SearchFilters


def test_languages_filter_emits_predicate():
    where, params = _filter_sql(SearchFilters(languages=["en", "de"]))
    assert "m.body_lang = ANY(" in where
    assert ["en", "de"] in params


def test_no_languages_filter_emits_nothing():
    where, _ = _filter_sql(SearchFilters())
    assert "body_lang" not in where


def test_languages_with_other_filters_combine():
    where, _ = _filter_sql(SearchFilters(languages=["en"], from_substr="alice"))
    assert "m.body_lang = ANY(" in where
    assert "m.from_addr ILIKE" in where or "m.from_name ILIKE" in where
