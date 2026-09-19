# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The pure rules behind `fields` and `snippet_chars` (kastellan slice E)."""
from __future__ import annotations

import pytest

from localmail.api.search import _to_api_result
from localmail.api.search_projection import (
    HIT_FIELDS,
    fields_error,
    project_hit,
    snippet_chars_error,
)
from localmail.search.searcher import SearchResult


def _result() -> SearchResult:
    return SearchResult(
        message_id=7, account_id=1, rank=1, score=0.5, rrf_score=0.5,
        subject="s", from_addr="a@b", from_name="A",
        date_sent=None, internal_date=None,
        snippet="the snippet", snippet_source="body",
        attachment_filename=None, has_attachments=False,
        matched_chunk_id=None, matched_chunk_table="message_chunks",
    )


def test_hit_fields_are_exactly_the_hit_keys_plus_snippet() -> None:
    # One authority: a key added to the hit later cannot go missing from the
    # projection, and no name is permitted that the hit never carries.
    assert set(HIT_FIELDS) == set(_to_api_result(_result())) | {"snippet"}
    assert len(HIT_FIELDS) == len(set(HIT_FIELDS))


def test_hit_fields_order_is_the_documented_one() -> None:
    assert HIT_FIELDS == (
        "message_id", "account", "folder", "subject", "from", "to", "date",
        "snippet_html", "has_attachments", "score", "matched_arms", "snippet",
    )


@pytest.mark.parametrize("name", HIT_FIELDS)
def test_every_documented_name_is_accepted(name: str) -> None:
    assert fields_error([name]) is None


def test_an_unknown_name_is_refused_by_name_with_the_supported_set() -> None:
    msg = fields_error(["message_id", "snippet_text"])
    assert msg is not None
    assert "'snippet_text'" in msg
    assert "message_id" in msg and "matched_arms" in msg  # the supported set


def test_several_unknown_names_are_all_named() -> None:
    msg = fields_error(["bogus", "nope"])
    assert msg is not None
    assert "'bogus'" in msg and "'nope'" in msg


def test_an_empty_list_is_refused() -> None:
    msg = fields_error([])
    assert msg is not None
    assert "empty" in msg


@pytest.mark.parametrize("bad", [[1], [None], ["message_id", 3.0]])
def test_a_non_string_element_is_refused(bad: list) -> None:
    msg = fields_error(bad)
    assert msg is not None
    assert "string" in msg


def test_a_non_list_is_refused() -> None:
    assert fields_error("message_id") is not None


def test_duplicates_are_accepted_and_collapse() -> None:
    assert fields_error(["score", "score"]) is None
    hit = _to_api_result(_result())
    assert project_hit(hit, ["score", "score"]) == {"score": 0.5}


def test_projection_returns_exactly_the_named_keys_in_canonical_order() -> None:
    hit = _to_api_result(_result())
    out = project_hit(hit, ["snippet", "message_id"])
    assert list(out) == ["message_id", "snippet"]
    assert out == {"message_id": "7", "snippet": "the snippet"}


def test_snippet_is_the_same_text_as_snippet_html() -> None:
    hit = _to_api_result(_result())
    out = project_hit(hit, ["snippet", "snippet_html"])
    assert out["snippet"] == out["snippet_html"] == "the snippet"


def test_projection_does_not_mutate_the_hit() -> None:
    hit = _to_api_result(_result())
    before = dict(hit)
    project_hit(hit, ["snippet"])
    assert hit == before
    assert "snippet" not in hit


def test_an_unvalidated_name_raises_rather_than_dropping() -> None:
    with pytest.raises(KeyError):
        project_hit(_to_api_result(_result()), ["bogus"])


@pytest.mark.parametrize("value", [1, 500, 1000])
def test_snippet_chars_in_range_is_accepted(value: int) -> None:
    assert snippet_chars_error(value, max_chars=1000) is None


@pytest.mark.parametrize("value", [0, -1, 1001])
def test_snippet_chars_out_of_range_names_the_range(value: int) -> None:
    msg = snippet_chars_error(value, max_chars=1000)
    assert msg is not None
    assert "1" in msg and "1000" in msg


@pytest.mark.parametrize("value", [True, False, 1.5, "10", None])
def test_snippet_chars_that_is_not_an_int_is_refused(value: object) -> None:
    # bool is an int subclass; True would otherwise read as 1.
    assert snippet_chars_error(value, max_chars=1000) is not None
