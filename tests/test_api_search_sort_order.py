# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""``sort_order`` at the api boundary: threading, refusal, and cursor minting."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import encode_keyset_cursor, keyset_order
from localmail.search.searcher import KeysetCursor, PoolMetadata

_KS = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100)


def _page(*, token=None, next_keyset=None):
    p = MagicMock()
    p.results = []
    p.search_token = token
    p.pool_size = 0
    p.page_size = 2
    p.page = 1
    p.has_more_in_pool = False
    p.can_grow_pool = False
    p.candidates_per_arm = 50
    p.timing_ms = {"total": 1.0}
    p.next_keyset = next_keyset
    p.rewrite_status = "not_requested"
    p.rewrite_note = None
    p.rewrite_note_code = None
    return p


def _searcher(page=None):
    s = MagicMock()
    s.config.candidates_per_arm = 50
    s.config.candidates_per_arm_max = 800
    s.smart_available = False
    s.search.return_value = page or _page()
    return s


def test_a_stated_order_reaches_the_searcher() -> None:
    s = _searcher()
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, sort="date",
               sort_order="asc")
    _, kwargs = s.search.call_args
    assert kwargs.get("sort_order") == "asc"


def test_an_unstated_order_reaches_the_searcher_as_desc() -> None:
    """Resolved at this boundary, from the one shared default."""
    s = _searcher()
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, sort="date")
    _, kwargs = s.search.call_args
    assert kwargs.get("sort_order") == "desc"


def test_rank_with_ascending_is_a_validation_error_not_a_search() -> None:
    s = _searcher()
    with pytest.raises(ValidationFailed, match="sort_order"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort="rank",
                   sort_order="asc")
    s.search.assert_not_called()


def test_rank_with_ascending_is_refused_even_with_an_empty_acl() -> None:
    """Validation precedes the empty-ACL short-circuit.

    That branch answers with an empty page, byte-identical to "you have
    reached the end" — so a grant-nothing caller would be told a
    contradictory request had succeeded and was complete.
    """
    s = _searcher()
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[], user_id=99, sort="rank",
                   sort_order="asc")


def test_an_ascending_page_mints_an_ascending_cursor() -> None:
    s = _searcher(_page(next_keyset=_KS))
    out = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                     allowed_account_ids=[1], user_id=99, sort="date",
                     sort_order="asc")
    assert keyset_order(out["next_cursor"]) == "asc", (
        "an ascending walk minted a descending cursor: the next page would "
        "silently reverse"
    )


def test_a_descending_page_mints_a_descending_cursor() -> None:
    s = _searcher(_page(next_keyset=_KS))
    out = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                     allowed_account_ids=[1], user_id=99, sort="date")
    assert keyset_order(out["next_cursor"]) == "desc"


def test_an_ascending_cursor_alone_continues_ascending() -> None:
    """The documented round trip, end to end through run_search."""
    s = _searcher()
    raw = encode_keyset_cursor(_KS, "asc")
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, cursor=raw)
    _, kwargs = s.search.call_args
    assert kwargs.get("sort") == "date"
    assert kwargs.get("sort_order") == "asc"
    assert kwargs.get("keyset_cursor") == _KS


def test_a_stated_order_contradicting_the_cursor_is_a_400() -> None:
    s = _searcher()
    raw = encode_keyset_cursor(_KS, "asc")
    with pytest.raises(ValidationFailed, match="sort_order"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort_order="desc",
                   cursor=raw)
    s.search.assert_not_called()


def test_a_pool_cursor_rejects_a_contradicting_order() -> None:
    s = _searcher()
    s.get_pool_metadata.return_value = PoolMetadata(
        candidates_per_arm=50, page_size=2, rerank_pool_size=100, pool_size=10,
        sort="rank", sort_order="desc",
    )
    with pytest.raises(ValidationFailed, match="sort_order"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort_order="asc",
                   cursor="tok-1:2")
