# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A query the Searcher cannot parse is a 400 on every branch, never a 500.

``run_search`` parses two strings before any work: the raw request field,
and the query composed from the caller's **unscoped** filters. The rowed
branches hand the Searcher a third: the query composed from the
**ACL-scoped** filters, which carries ``account_id:`` tokens the other two
lack. An unclosed quote at the end of the free text swallows those tokens,
so a value both gates read as empty reaches ``_parse_has`` as
``' account_id:1'`` and raises a bare ``QueryParseError``. Neither branch's
``except SearchArgumentRefused`` catches it, and ``serve.app`` answers it as
a 500 with no problem+json body.

``has:"`` answered 200 before #364 made an unknown ``has:`` value raise;
``after:"`` has been this 500 for longer. Both are driven through a real
``Searcher``, because the property is that the two parses genuinely
disagree — a mock would only prove the plumbing.

The refusal names the open quote rather than the parser's complaint, which
quoted the swallowed ``account_id:3 account_id:7`` tokens back at a caller
who never wrote them. The silent case, where the swallowed text parses, is
#367.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import encode_keyset_cursor
from localmail.config import SearchConfig
from localmail.search.query import unclosed_quote
from localmail.search.searcher import KeysetCursor, Searcher


class _Embeddings:
    name = "s"
    model = "s"
    dimension = 768

    def embed_documents(self, texts):  # pragma: no cover - never reached
        raise AssertionError("retrieval must not start")

    def embed_query(self, text):  # pragma: no cover - never reached
        raise AssertionError("retrieval must not start")

    def health_check(self) -> None:
        pass


def _searcher() -> Searcher:
    pool = MagicMock()
    # Reaching this means the query parsed, i.e. was not refused.
    pool.connection.side_effect = AssertionError("retrieval was reached")
    return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embeddings(),
                    reranker=None, rewriter=None)


_TEXT_WALK_CURSOR = encode_keyset_cursor(KeysetCursor(
    ts=datetime(2026, 1, 1, tzinfo=timezone.utc), id=5, order="desc", walk="text",
))


_UNCLOSED = ['has:"', 'invoice has:"', 'invoice after:"2024-01-01',
             'invoice before:"', "invoice has:'attachment"]
_NAMES_THE_QUOTE = "query: unclosed .* quote"


def _refusal(**kwargs) -> str:
    with pytest.raises(ValidationFailed, match=_NAMES_THE_QUOTE) as info:
        run_search(searcher=_searcher(), limit=5, user_id=1, **kwargs)
    message = str(info.value)
    assert "account_id" not in message, message
    return message


@pytest.mark.parametrize("free_text", _UNCLOSED)
def test_the_fresh_branch_refuses_it(free_text: str) -> None:
    _refusal(free_text=free_text, filters={}, allowed_account_ids=[3, 7])


@pytest.mark.parametrize("free_text", _UNCLOSED)
def test_the_keyset_branch_refuses_it(free_text: str) -> None:
    """``has:"`` included: its free text is ``has:``, which is not blank, so
    the text-walk cursor's own guard (#326) never fires first."""
    _refusal(free_text=free_text, filters={}, allowed_account_ids=[3, 7],
             cursor=_TEXT_WALK_CURSOR)


@pytest.mark.parametrize("allowed", [[3, 7], []])
def test_the_early_gate_names_the_quote_when_the_caller_s_filters_are_swallowed(
    allowed,
) -> None:
    """The early composed gate has the same shape with the caller's own
    filters: ``invoice after:"`` + ``subject`` used to answer
    ``after: expected YYYY-MM-DD, got ' subject:x'``. ``[]`` because that
    gate runs ahead of the empty-ACL short-circuit."""
    message = _refusal(free_text='invoice after:"', filters={"subject": "x"},
                       allowed_account_ids=allowed)
    assert "subject" not in message, message


def test_a_query_that_is_malformed_on_its_own_keeps_the_parser_s_message() -> None:
    """The quote is named only when it is what breaks the composition. A bad
    date is the caller's own error with or without the quote, so that is
    what they are told."""
    with pytest.raises(ValidationFailed, match="after: expected YYYY-MM-DD"):
        run_search(searcher=_searcher(), free_text='invoice after:"2024-13-01',
                   filters={}, limit=5, allowed_account_ids=[1], user_id=1)


def test_a_contradiction_without_a_quote_keeps_the_parser_s_message() -> None:
    with pytest.raises(ValidationFailed, match="contradict each other"):
        run_search(searcher=_searcher(), free_text="invoice has:attachment",
                   filters={"has_attachment": False}, limit=5,
                   allowed_account_ids=[1], user_id=1)


@pytest.mark.parametrize("text, quote", [
    ("invoice", None),
    ('"quoted phrase" here', None),
    ("O'Brien", "'"),
    ('has:"', '"'),
    ("""it's "open""", "'"),
])
def test_unclosed_quote_reports_the_quote_left_open(text, quote) -> None:
    assert unclosed_quote(text) == quote


def test_a_balanced_query_still_reaches_retrieval_on_both_branches() -> None:
    """Positive control: the gate must not refuse what the Searcher parses."""
    with pytest.raises(AssertionError, match="retrieval was reached"):
        run_search(searcher=_searcher(), free_text='invoice has:"attachment"',
                   filters={}, limit=5, allowed_account_ids=[1], user_id=1)
    with pytest.raises(AssertionError, match="retrieval was reached"):
        run_search(searcher=_searcher(), free_text="invoice", filters={},
                   limit=5, allowed_account_ids=[1], user_id=1,
                   cursor=_TEXT_WALK_CURSOR)
