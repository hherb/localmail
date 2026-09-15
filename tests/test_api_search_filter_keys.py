# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unknown filter keys are refused by name, before the empty-ACL page (#364).

``extra: "ignore"`` used to drop them, and the docstring called that forward
compatibility. It was the opposite: a planner copying the hit field's
plural (``has_attachments``) back as a filter got unfiltered results and a
200, and so did a newer client talking to an older server.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import _SUPPORTED_FILTER_KEYS, filter_key_error, run_search

_SUPPORTED = ("account_ids, after, before, date_from, date_to, folder_ids, from, "
              "has_attachment, lang, subject, to")


def _searcher() -> MagicMock:
    """A searcher that fails the test if the refusal did not precede it."""
    searcher = MagicMock()
    searcher.smart_available = False
    searcher.search.side_effect = AssertionError("retrieval must not start")
    return searcher


def test_every_supported_key_passes() -> None:
    assert filter_key_error({k: None for k in _SUPPORTED_FILTER_KEYS}) is None


def test_the_supported_list_in_the_message_is_the_real_set() -> None:
    assert ", ".join(sorted(_SUPPORTED_FILTER_KEYS)) == _SUPPORTED


def test_an_unknown_key_is_named_with_a_suggestion() -> None:
    assert filter_key_error({"has_attachments": True}) == (
        "filters: unknown key 'has_attachments' (did you mean 'has_attachment'?); "
        f"supported: {_SUPPORTED}"
    )


def test_an_unknown_key_with_no_close_match_gets_no_suggestion() -> None:
    assert filter_key_error({"colour": "red"}) == (
        f"filters: unknown key 'colour'; supported: {_SUPPORTED}"
    )


def test_a_null_valued_unknown_key_is_still_refused() -> None:
    """A key that does not exist is a mistake whatever its value."""
    assert filter_key_error({"has_attachments": None}) is not None


def test_several_unknown_keys_are_all_named() -> None:
    assert filter_key_error({"zeta": 1, "has_attachments": True}) == (
        "filters: unknown keys 'has_attachments' (did you mean 'has_attachment'?), "
        f"'zeta'; supported: {_SUPPORTED}"
    )


@pytest.mark.parametrize("allowed", [[1], []])
def test_run_search_refuses_an_unknown_key_before_any_work(allowed) -> None:
    """``[]`` is the case that matters: that branch answers an empty page,
    which reads as "no results" rather than "your request was wrong"."""
    with pytest.raises(ValidationFailed, match="unknown key 'has_attachments'"):
        run_search(searcher=_searcher(), free_text="flight",
                   filters={"has_attachments": True}, limit=10,
                   allowed_account_ids=allowed, user_id=1)


@pytest.mark.parametrize("filters, message", [
    ({"date_from": "last-week"}, "date_from: expected YYYY-MM-DD"),
    ({"lang": ""}, "lang: empty value not allowed"),
    ({"account_ids": ["x1"]}, "account_id must be a base-10 integer"),
    ({"has_attachment": "yes"}, "has_attachment: expected true, false or null"),
])
def test_a_malformed_filter_value_is_a_400_even_with_an_empty_acl(filters, message) -> None:
    """These fail before #364: the value checks ran inside
    ``build_query_string``, below the branch that had already answered.
    Each case names the check that must fire, so an unrelated earlier
    ``ValidationFailed`` cannot satisfy it."""
    with pytest.raises(ValidationFailed, match=message):
        run_search(searcher=_searcher(), free_text="flight", filters=filters,
                   limit=10, allowed_account_ids=[], user_id=1)


def test_a_well_formed_request_with_an_empty_acl_still_answers_an_empty_page() -> None:
    """Positive control: the gate must not refuse what it should pass."""
    page = run_search(searcher=_searcher(), free_text="flight",
                      filters={"has_attachment": False, "date_from": "2026-01-01"},
                      limit=10, allowed_account_ids=[], user_id=1)
    assert page["results"] == []
    assert page["next_cursor"] is None


@pytest.mark.parametrize("allowed", [[1], []])
@pytest.mark.parametrize("free_text,filters", [
    ("invoice has:attachment", {"has_attachment": False}),
    ("invoice has:no-attachment", {"has_attachment": True}),
])
def test_a_has_token_in_the_query_contradicting_the_filter_is_refused(
    free_text, filters, allowed,
) -> None:
    """The contradiction exists only in the composed string (#364 F1): the
    free-text `has:` token and the structured `has_attachment` filter each
    look fine in isolation, and only `build_query_string`'s concatenation
    of the two puts both tokens in front of `parse_query` at once. `[]`
    matters as much as `[1]` — the empty-ACL branch must not report this
    as a completed, contentless search."""
    with pytest.raises(ValidationFailed,
                       match="has: 'attachment' and 'no-attachment' contradict each other"):
        run_search(searcher=_searcher(), free_text=free_text, filters=filters,
                   limit=10, allowed_account_ids=allowed, user_id=1)


def test_a_has_token_agreeing_with_the_filter_is_not_refused() -> None:
    """Positive control: an agreeing `has:` + `has_attachment` pair must
    still reach the empty-ACL short-circuit rather than being refused."""
    page = run_search(searcher=_searcher(), free_text="invoice has:attachment",
                      filters={"has_attachment": True}, limit=10,
                      allowed_account_ids=[], user_id=1)
    assert page["results"] == []
    assert page["next_cursor"] is None
