"""Cursor + transparent pool growth tests for localmail.api.search.run_search."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from localmail.api.errors import SearchCursorExpired, ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import encode_search_cursor, SearchCursor
from localmail.search.page_cache import CacheMissError


def _result(message_id: int = 1) -> MagicMock:
    r = MagicMock()
    r.message_id = message_id
    r.account_id = 1
    r.rank = 1
    r.score = 0.5
    r.rrf_score = 0.4
    r.subject = "s"
    r.from_addr = "a@x"
    r.from_name = "A"
    r.date_sent = None
    r.snippet = ""
    r.snippet_source = "body"
    r.attachment_filename = None
    r.matched_chunk_table = "message_chunks"
    return r


def _page(*, results: list, token: str | None, pool_size: int,
          page_size: int, has_more: bool, can_grow: bool,
          page: int = 1) -> MagicMock:
    p = MagicMock()
    p.results = results
    p.search_token = token
    p.pool_size = pool_size
    p.page_size = page_size
    p.page = page
    p.has_more_in_pool = has_more
    p.can_grow_pool = can_grow
    # candidates_per_arm is what the route inspects vs cfg.candidates_per_arm_max
    p.candidates_per_arm = 50
    p.timing_ms = {"total": 1.0}
    return p


def test_initial_search_emits_next_cursor_when_more_in_pool() -> None:
    s = MagicMock()
    s.search.return_value = _page(
        results=[_result()], token="tok-1", pool_size=10,
        page_size=2, has_more=True, can_grow=True,
    )
    out = run_search(searcher=s, free_text="hello", filters={},
                     limit=2, allowed_account_ids=[1], user_id=99)
    assert out["next_cursor"] == "tok-1:2"
    assert len(out["results"]) == 1
    s.search.assert_called_once()


def test_initial_search_emits_null_cursor_when_pool_exhausted() -> None:
    s = MagicMock()
    s.search.return_value = _page(
        results=[_result()], token="tok-1", pool_size=2,
        page_size=2, has_more=False, can_grow=False,
    )
    out = run_search(searcher=s, free_text="hello", filters={},
                     limit=2, allowed_account_ids=[1], user_id=99)
    assert out["next_cursor"] is None


def test_cursor_dispatches_to_continue_page() -> None:
    s = MagicMock()
    s.continue_page.return_value = _page(
        results=[_result(2)], token="tok-1", pool_size=10,
        page_size=2, has_more=True, can_grow=True, page=2,
    )
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=2))
    out = run_search(searcher=s, free_text="hello", filters={},
                     limit=2, allowed_account_ids=[1], user_id=99,
                     cursor=cursor)
    s.search.assert_not_called()
    s.continue_page.assert_called_once_with("tok-1", 2, user_id=99)
    assert out["next_cursor"] == "tok-1:3"


def test_cache_miss_raises_search_cursor_expired() -> None:
    s = MagicMock()
    s.continue_page.side_effect = CacheMissError("tok-1")
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=2))
    with pytest.raises(SearchCursorExpired):
        run_search(searcher=s, free_text="hello", filters={},
                   limit=2, allowed_account_ids=[1], user_id=99,
                   cursor=cursor)


def test_pool_exhausted_with_grow_pool_available_triggers_grow_pool() -> None:
    """When the cursor's page would land past the pool but can_grow_pool=True,
    the route calls grow_pool(token, candidates_per_arm*2) and returns the
    resulting page."""
    from localmail.search.page_cache import PageOutOfPoolError
    s = MagicMock()
    # The cached pool currently has cpa=50; advancing the cursor past it.
    s.continue_page.side_effect = PageOutOfPoolError("past pool")
    # Set up the searcher's _cache.get to return an entry with current cpa
    cache_entry = {
        "candidates_per_arm": 50,
        "page_size": 2,
        "rerank_pool_size": 20,
    }
    s._cache.get.return_value = cache_entry
    # The fake's _cfg needs candidates_per_arm and candidates_per_arm_max.
    s._cfg.candidates_per_arm = 50
    s._cfg.candidates_per_arm_max = 800
    # grow_pool returns a freshly enlarged pool's page 1.
    grown_page = _page(
        results=[_result(3)], token="tok-2", pool_size=20,
        page_size=2, has_more=True, can_grow=True,
    )
    s.grow_pool.return_value = grown_page
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=5))
    out = run_search(searcher=s, free_text="hello", filters={},
                     limit=2, allowed_account_ids=[1], user_id=99,
                     cursor=cursor)
    s.grow_pool.assert_called_once()
    args, kwargs = s.grow_pool.call_args
    # Token first arg; new cpa > 50
    assert args[0] == "tok-1"
    new_cpa = args[1] if len(args) > 1 else kwargs.get("candidates_per_arm")
    assert new_cpa > 50
    assert out["next_cursor"] == "tok-2:2"


def test_malformed_cursor_raises_validation_failed() -> None:
    s = MagicMock()
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="x", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99,
                   cursor="not-a-cursor")
