# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A malformed operator value is a 400, never an unhandled 500 (#333 review).

``run_search`` parses ``free_text`` at its cursor gate, which runs *before*
the empty-ACL short-circuit and *on* the pool-cursor branch — two paths that
never parsed it until #326 gave the gate a ``free_text`` argument. The parser
raises ``QueryParseError``, a bare ``ValueError`` that no ``api/`` or
``serve/`` handler catches: ``serve.app`` registers one for ``APIError``
only, so it escaped as a 500 with no problem+json body, and reached the MCP
tool as an unmapped exception.

``query="invoice after:last-week"`` is exactly the shape an LLM agent emits,
which is the audience this whole cursor cluster is written for.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import SearchCursor, encode_search_cursor
from localmail.search.searcher import KeysetCursor, PoolMetadata

#: Both raising shapes ``search.query`` has: a date it cannot parse, and an
#: operator whose value is empty.
MALFORMED = ["invoice after:last-week", "invoice before:soon", "lang:"]


def _searcher() -> MagicMock:
    """A searcher that fails the test if the gate lets a bad query past."""
    s = MagicMock()
    s.search.side_effect = AssertionError("retrieval must not start")
    s.continue_page.side_effect = AssertionError("retrieval must not start")
    return s


@pytest.mark.parametrize("free_text", MALFORMED)
def test_a_malformed_operator_is_a_400_even_with_an_empty_acl(
    free_text: str,
) -> None:
    """The empty-ACL branch answers with an empty page, so it must not be
    reached: a caller granted nothing would otherwise be told a malformed
    request had succeeded and was complete — the reason every other guard
    here is ordered ahead of that short-circuit."""
    with pytest.raises(ValidationFailed):
        run_search(searcher=_searcher(), free_text=free_text, filters={},
                   limit=10, allowed_account_ids=[], user_id=1)


@pytest.mark.parametrize("free_text", MALFORMED)
def test_a_malformed_operator_is_a_400_on_a_pool_continuation(
    free_text: str,
) -> None:
    """The pool branch reuses the pool's cached parse and never needed the
    query re-parsed; the gate parses it anyway, so it must fail cleanly."""
    searcher = _searcher()
    searcher.get_pool_metadata.return_value = PoolMetadata(
        candidates_per_arm=50, page_size=10, rerank_pool_size=10,
        pool_size=10, sort="rank", sort_order="desc",
    )
    cursor = encode_search_cursor(SearchCursor(token="t", page=2))
    with pytest.raises(ValidationFailed):
        run_search(searcher=searcher, free_text=free_text, filters={},
                   limit=10, allowed_account_ids=[1], user_id=1, cursor=cursor)


@pytest.mark.parametrize("free_text", MALFORMED)
def test_a_malformed_operator_is_a_400_on_a_fresh_search(
    free_text: str,
) -> None:
    """The pre-existing half: ``Searcher.search`` parses too, so this path
    raised the same bare ``ValueError`` before #326 existed. Fixed at the
    gate, which now precedes it for every caller of this function."""
    with pytest.raises(ValidationFailed):
        run_search(searcher=_searcher(), free_text=free_text, filters={},
                   limit=10, allowed_account_ids=[1], user_id=1)


def test_a_well_formed_query_still_reaches_retrieval() -> None:
    """The positive control: a gate that refused everything would satisfy
    every assertion above."""
    searcher = MagicMock()
    searcher.smart_available = False
    with pytest.raises(AssertionError, match="reached retrieval"):
        searcher.search.side_effect = AssertionError("reached retrieval")
        run_search(searcher=searcher, free_text="invoice after:2026-01-01",
                   filters={}, limit=10, allowed_account_ids=[1], user_id=1)


def test_a_malformed_operator_is_a_400_on_a_keyset_continuation() -> None:
    """The keyset branch decodes its cursor at the same gate, so the two
    refusals must not race: whichever fires, it is a 400."""
    from localmail.api.search_cursor import encode_keyset_cursor

    ks = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100,
                      order="desc", walk="archive")
    with pytest.raises(ValidationFailed):
        run_search(searcher=_searcher(), free_text="x after:last-week",
                   filters={}, limit=10, allowed_account_ids=[1], user_id=1,
                   cursor=encode_keyset_cursor(ks))
