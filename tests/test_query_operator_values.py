# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""An operator with an empty or unparsable value (#378).

Such a token used to fall back to free text: neither applied nor refused,
against #364's "honoured or refused, never dropped". The operator's name then
became an FTS term (``has:"`` searched for the word "has") and flipped the
query to rankable.

Refused only for the operators whose value has a fixed shape (a date, an id,
``has:``'s two values), since no ordinary text spells one of those. The text
operators keep an empty value as free text on purpose: ``subject: meeting``
and ``reply to: bob`` are what people and LLM agents type, and refusing them
would 400 ordinary queries.
"""
from __future__ import annotations

import pytest

from localmail.search.query import SearchFilters, parse_query, QueryParseError

#: The fixed-shape operators, each with no value.
_EMPTY = ["after:", "before:", "has:", "account_id:", "folder_id:"]


@pytest.mark.parametrize("token", _EMPTY)
def test_an_empty_fixed_shape_operator_is_refused(token: str) -> None:
    op = token.rstrip(":")
    with pytest.raises(QueryParseError, match=rf"^{op}: empty value not allowed$"):
        parse_query(f"invoice {token}")


@pytest.mark.parametrize("query", ['has:"', 'invoice before:"', "AFTER:"])
def test_the_reproductions_from_378_are_refused(query: str) -> None:
    """An open quote leaves the value empty; the operator name is matched
    case-insensitively, as everywhere else in the parser."""
    with pytest.raises(QueryParseError, match="empty value not allowed"):
        parse_query(query)


@pytest.mark.parametrize("query", ["account_id:abc invoice", "folder_id:x",
                                   "account_id:1.5", "folder_id:\"7 8\""])
def test_a_non_integer_id_is_refused(query: str) -> None:
    op = query.partition(":")[0]
    with pytest.raises(QueryParseError, match=rf"^{op}: expected an integer id, got "):
        parse_query(query)


@pytest.mark.parametrize("query", ["subject: meeting", "reply to: bob",
                                   "from: alice", "label:", "account:",
                                   "folder:", "attachment_name:"])
def test_an_empty_text_operator_stays_free_text(query: str) -> None:
    """The deliberate half: these read as ordinary text."""
    q = parse_query(query)
    assert q.free_text == query
    assert q.filters == SearchFilters()


def test_valid_fixed_shape_operators_still_apply() -> None:
    """Positive control: a rule refusing too broadly fails here."""
    f = parse_query("account_id:3 folder_id:7 after:2024-01-01 "
                    "before:2024-02-01 has:attachment invoice").filters
    assert (f.account_ids, f.folder_ids) == ([3], [7])
    assert f.has_attachment is True
    assert f.after is not None and f.before is not None
