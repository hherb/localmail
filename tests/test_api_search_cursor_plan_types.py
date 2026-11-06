# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""#327: one plan type per cursor mode, so a pool plan cannot claim an ordering.

``CursorPlan`` was one class carrying ``mode``, ``sort`` and ``sort_order``,
and for ``mode="pool"`` the two ordering fields were a resolved *default*
describing nothing: ``continue_page`` serves whatever the pool was built
with. One consumer read them anyway — ``run_search``'s rank+asc refusal —
so a pool cursor paged with ``sort_order="asc"`` was refused for naming
``sort='rank' (the default)``, a sort the caller never stated, before the
pool was ever consulted. Right only while every pool is rank-built, which is
the invariant ``PoolMetadata.sort`` exists not to assume.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import (
    FreshPlan,
    KeysetPlan,
    PoolPlan,
    SearchCursor,
    encode_keyset_cursor,
    resolve_cursor_plan,
)
from localmail.search.searcher import KeysetCursor, PoolMetadata
from localmail.search.sort_axes import sort_order_applicability_error

_KS = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100,
                   order="asc", walk="text")


def _plan(cursor, sort=None, sort_order=None, free_text="invoice"):
    return resolve_cursor_plan(cursor=cursor, requested_sort=sort,
                               requested_sort_order=sort_order,
                               free_text=free_text)


# --- the types -------------------------------------------------------------

def test_no_cursor_is_a_fresh_plan_with_its_resolved_ordering() -> None:
    assert _plan(None) == FreshPlan(sort="rank", sort_order="desc")


def test_a_keyset_cursor_is_a_keyset_plan_carrying_the_decoded_cursor() -> None:
    plan = _plan(encode_keyset_cursor(_KS))
    assert plan == KeysetPlan(cursor=_KS)
    assert (plan.sort, plan.sort_order) == ("date", "asc")


def test_a_pool_cursor_is_a_pool_plan_carrying_only_what_was_stated() -> None:
    assert _plan("tok-1:2") == PoolPlan(
        cursor=SearchCursor(token="tok-1", page=2),
        stated_sort=None, stated_sort_order=None)
    assert _plan("tok-1:2", sort="date", sort_order="asc") == PoolPlan(
        cursor=SearchCursor(token="tok-1", page=2),
        stated_sort="date", stated_sort_order="asc")


def test_a_pool_plan_cannot_be_asked_what_ordering_will_run() -> None:
    """It does not know: the pool does. An attribute that answered anyway is
    the defect, so its absence is the pin."""
    plan = _plan("tok-1:2")
    assert not hasattr(plan, "sort")
    assert not hasattr(plan, "sort_order")


def test_a_malformed_pool_cursor_is_refused_by_the_resolver() -> None:
    """Decoded where the keyset payload already was, so it is refused ahead
    of the empty-ACL short-circuit like its sibling."""
    with pytest.raises(ValidationFailed, match="cursor"):
        _plan("tok-1:0")


# --- through run_search ----------------------------------------------------

def _pool_searcher(*, sort: str, sort_order: str) -> MagicMock:
    s = MagicMock()
    s.config.candidates_per_arm_max = 800
    s.smart_available = False
    s.search.side_effect = AssertionError("a pool cursor never re-searches")
    s.get_pool_metadata.return_value = PoolMetadata(
        candidates_per_arm=50, page_size=2, rerank_pool_size=100,
        pool_size=10, sort=sort, sort_order=sort_order, rankable=True)
    page = MagicMock()
    page.results, page.search_token, page.next_keyset = [], "tok-1", None
    page.has_more_in_pool = page.can_grow_pool = False
    page.sort_applied, page.rankable = sort, True
    page.timing_ms = {"total": 1.0}
    s.continue_page.return_value = page
    return s


def test_an_order_matching_an_ascending_pool_continues() -> None:
    """The issue's own scenario. The pool says ascending, the caller says
    ascending: there is nothing to refuse, and the old rank+asc refusal
    refused it anyway, from a default ``sort`` nobody stated."""
    s = _pool_searcher(sort="date", sort_order="asc")
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=1, sort_order="asc",
               cursor="tok-1:2")
    s.continue_page.assert_called_once()


def test_an_ascending_order_against_a_rank_pool_names_the_pool_s_order() -> None:
    """Still a 400 — but judged against the pool, so the remedy names the
    ordering the pool actually has rather than a default."""
    s = _pool_searcher(sort="rank", sort_order="desc")
    with pytest.raises(ValidationFailed,
                       match="continues a descending search") as exc:
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=1, sort_order="asc",
                   cursor="tok-1:2")
    assert "(the default)" not in str(exc.value)
    s.get_pool_metadata.assert_called_once()


def test_the_empty_acl_branch_resolves_a_pool_plan_from_what_was_stated() -> None:
    """No rows come back, so ``sort_applied`` describes a pool this branch
    never reads — resolved, as before #327, from the stated sort. A plan
    that answered with a default instead reports ``rank`` to a caller who
    said ``date``."""
    s = _pool_searcher(sort="rank", sort_order="desc")
    out = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                     allowed_account_ids=[], user_id=1, sort="date",
                     cursor="tok-1:2")
    assert out["results"] == []
    assert out["sort_applied"] == "date"
    s.get_pool_metadata.assert_not_called()


# --- review of #327: what stays ahead of the empty-ACL short-circuit -------

def test_a_stated_rank_asc_on_a_pool_cursor_is_refused_without_the_pool() -> None:
    """No pool is rank-built ascending (a fresh rank + asc is refused), so the
    stated pair contradicts every pool and needs no cache probe. Refused for
    a grant-nothing caller, whose empty page would read as "end of results",
    and for a granted one without consulting the pool, which if evicted would
    have answered a misleading 409."""
    for acl in ([], [1]):
        s = _pool_searcher(sort="rank", sort_order="desc")
        with pytest.raises(ValidationFailed) as exc:
            run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                       allowed_account_ids=acl, user_id=1, sort="rank",
                       sort_order="asc", cursor="tok-1:2")
        assert str(exc.value) == sort_order_applicability_error(
            sort="rank", sort_order="asc", continuing_pool=True)
        s.get_pool_metadata.assert_not_called()


def test_a_stated_asc_alone_on_a_pool_cursor_is_left_to_the_pool() -> None:
    """Deliberate: with no stated sort, only the pool can say whether ``asc``
    contradicts it, and a grant-nothing caller never reaches the pool."""
    s = _pool_searcher(sort="rank", sort_order="desc")
    out = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                     allowed_account_ids=[], user_id=1, sort_order="asc",
                     cursor="tok-1:2")
    assert out["results"] == []
    s.get_pool_metadata.assert_not_called()


def test_a_malformed_pool_cursor_is_refused_ahead_of_the_empty_acl_branch() -> None:
    s = _pool_searcher(sort="rank", sort_order="desc")
    with pytest.raises(ValidationFailed, match="page"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[], user_id=1, cursor="tok-1:0")


def test_the_fresh_refusal_is_the_shared_rule_s_wording() -> None:
    s = _pool_searcher(sort="rank", sort_order="desc")
    with pytest.raises(ValidationFailed) as exc:
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[], user_id=1, sort_order="asc")
    assert str(exc.value) == sort_order_applicability_error(
        sort="rank", sort_order="asc", continuing_pool=False)
