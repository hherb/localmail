# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The pure rule behind `fields` (kastellan slice E).

`snippet_chars` moved out in #390: its type and floor are
`search.snippet_width.snippet_width_error`, shared with the Searcher, and
this boundary supplies only the cap. Its tests are in
tests/test_snippet_width.py.
"""
from __future__ import annotations

import pytest

from localmail.api.search import _to_api_result
from localmail.api.search_projection import (
    HIT_FIELDS,
    fields_error,
    project_hit,
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
    # Positional, because the module docstring's claim is positional ("the
    # first eleven"). The set comparison above admits a reorder of
    # `_to_api_result`, after which a projected hit's key order diverges from
    # a default hit's and the claim is false with both assertions green.
    assert tuple(HIT_FIELDS[:-1]) == tuple(_to_api_result(_result()))
    assert HIT_FIELDS[-1] == "snippet"


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
    with pytest.raises(ValueError, match="bogus"):
        project_hit(_to_api_result(_result()), ["bogus"])


def test_an_empty_fields_raises_rather_than_returning_a_keyless_hit() -> None:
    """#392: the guard re-derived a *subset* of `fields_error`'s judgement
    by hand — names outside `HIT_FIELDS`, and nothing else — so the one
    shape `fields_error` explicitly calls "a caller bug, not a request"
    passed straight through and produced hits carrying no keys at all."""
    with pytest.raises(ValueError, match="empty"):
        project_hit(_to_api_result(_result()), [])


def test_the_precondition_is_the_gate_s_own_rule_not_a_restatement() -> None:
    """Differential: `project_hit` raises iff `fields_error` reports, with
    that message. The hand-written guard agreed with it on unknown names
    and on nothing else."""
    for fields in ([], ["bogus"], ["score", "bogus"], ["message_id", "snippet"]):
        expected = fields_error(list(fields))
        if expected is None:
            project_hit(_to_api_result(_result()), fields)
            continue
        with pytest.raises(ValueError) as excinfo:
            project_hit(_to_api_result(_result()), fields)
        assert str(excinfo.value) == expected


def test_several_unknown_names_are_all_reported() -> None:
    """The old guard iterated a `set` and raised `KeyError` on an
    arbitrary member, so a caller with two typos was told about one of
    them, unpredictably."""
    with pytest.raises(ValueError) as excinfo:
        project_hit(_to_api_result(_result()), ["nope", "alsonope"])
    assert "nope" in str(excinfo.value) and "alsonope" in str(excinfo.value)


def test_a_non_list_sequence_is_still_accepted() -> None:
    """The parameter is a `Sequence`, and `fields_error` requires a `list`
    — so the delegation must normalise rather than refuse a tuple."""
    out = project_hit(_to_api_result(_result()), ("score",))
    assert out == {"score": 0.5}


def test_a_one_shot_iterable_is_projected_not_silently_emptied() -> None:
    """`fields` is traversed exactly once.

    The delegation's first cut read `list(fields)` for the check and
    `set(fields)` for the projection. A generator is drained by the first,
    so the second saw nothing and the comprehension returned `{}` — a hit
    with no keys, silently, which is verbatim the outcome #392 removed for
    an empty list. Not reachable from the wire (`run_search` passes the
    pydantic `list[str]`), and the annotation is `Sequence[str]`, but CI
    runs no mypy step so the annotation gates nothing.

    The guard #392 replaced was single-traversal by accident — it read
    `set(fields)` first and iterated *that*. This pin is what makes the
    delegation single-traversal on purpose.
    """
    assert project_hit(_to_api_result(_result()), iter(["score"])) == {"score": 0.5}
    assert project_hit(
        _to_api_result(_result()), (n for n in ("subject", "score"))
    ) == {"subject": "s", "score": 0.5}


def test_a_one_shot_iterable_of_unknown_names_is_still_refused() -> None:
    """The refusal path traverses too, so it must read the same binding."""
    with pytest.raises(ValueError, match="bogus"):
        project_hit(_to_api_result(_result()), iter(["bogus"]))


# `snippet_chars` is no longer ruled on here: since #390 its type and floor
# are `search.snippet_width.snippet_width_error`, shared with the Searcher,
# and this boundary supplies only the cap. Its tests live in
# tests/test_snippet_width.py.
