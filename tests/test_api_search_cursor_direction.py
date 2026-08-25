# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A keyset cursor carries the direction it continues (#308, new axis).

The cursor used to carry only ``(ts, id)``. Paging an ascending search the
documented way — send ``next_cursor`` back and state nothing else — would
resolve the unstated ``sort_order`` to ``desc`` and silently continue
backwards: page 1 of a differently ordered search wearing a continuation's
clothes, which looks right until the results repeat.

``K|`` keeps its meaning (descending), so no cursor in flight breaks and
``api.browse_cursor``'s shared payload encoding is untouched.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search_cursor import (
    CursorPlan,
    decode_keyset_cursor,
    encode_keyset_cursor,
    is_keyset_cursor,
    keyset_order,
    reject_pool_sort_mismatch,
    resolve_cursor_plan,
)
from localmail.search.searcher import KeysetCursor

#: An archive-walk position: these tests send no query, and an
#: archive cursor is the flavour that continues under any query (#326).
_KS = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100,
                   order="desc", walk="archive")


def test_the_two_directions_mint_different_prefixes() -> None:
    assert encode_keyset_cursor(replace(_KS, order="desc")).startswith("K|")
    assert encode_keyset_cursor(replace(_KS, order="asc")).startswith("KA|")


def test_both_prefixes_are_keyset_cursors_and_round_trip() -> None:
    """Position *and* direction survive the round trip.

    The direction used to be dropped on decode — the wire carried it, the
    ``KeysetCursor`` handed to the Searcher did not, and the Searcher then
    defaulted it to "desc". Asserting equality against the whole cursor is
    what makes that loss visible here rather than three layers down.
    """
    for order in ("asc", "desc"):
        expected = replace(_KS, order=order)
        raw = encode_keyset_cursor(expected)
        assert is_keyset_cursor(raw)
        assert keyset_order(raw) == order
        assert decode_keyset_cursor(raw) == expected


def test_a_legacy_cursor_still_means_descending() -> None:
    """Cursors minted before this change carry no marker and must not flip."""
    legacy = encode_keyset_cursor(replace(_KS, order="desc"))
    assert keyset_order(legacy) == "desc"
    plan = resolve_cursor_plan(cursor=legacy, requested_sort=None,
                               requested_sort_order=None, free_text="")
    assert plan == CursorPlan(mode="keyset", sort="date", sort_order="desc")


def test_an_ascending_cursor_alone_continues_ascending() -> None:
    """The documented way to page: send the cursor, state nothing else."""
    raw = encode_keyset_cursor(replace(_KS, order="asc"))
    plan = resolve_cursor_plan(cursor=raw, requested_sort=None,
                               requested_sort_order=None, free_text="")
    assert plan == CursorPlan(mode="keyset", sort="date", sort_order="asc")


def test_a_stated_order_contradicting_the_cursor_is_rejected() -> None:
    raw = encode_keyset_cursor(replace(_KS, order="asc"))
    with pytest.raises(ValidationFailed, match="sort_order"):
        resolve_cursor_plan(cursor=raw, requested_sort=None,
                            requested_sort_order="desc", free_text="")


def test_a_stated_order_agreeing_with_the_cursor_is_accepted() -> None:
    raw = encode_keyset_cursor(replace(_KS, order="asc"))
    plan = resolve_cursor_plan(cursor=raw, requested_sort="date",
                               requested_sort_order="asc", free_text="")
    assert plan.sort_order == "asc"


def test_a_fresh_request_resolves_both_defaults() -> None:
    plan = resolve_cursor_plan(cursor=None, requested_sort=None,
                               requested_sort_order=None, free_text="")
    assert plan == CursorPlan(mode="fresh", sort="rank", sort_order="desc")


def test_a_fresh_request_keeps_what_the_caller_stated() -> None:
    plan = resolve_cursor_plan(cursor=None, requested_sort="date",
                               requested_sort_order="asc", free_text="")
    assert plan == CursorPlan(mode="fresh", sort="date", sort_order="asc")


def test_a_pool_cursor_reports_the_pool_mode() -> None:
    plan = resolve_cursor_plan(cursor="tok-1:2", requested_sort=None,
                               requested_sort_order=None, free_text="")
    assert plan.mode == "pool"


# --- the pool guard's order half, which no request can reach --------------
#
# A pool cursor whose stated order is "asc" is answered by `run_search`'s
# rank+asc refusal long before the pool is probed, and getting past that
# refusal forces `requested_sort="date"`, which the sort half then rejects
# against a rank-built pool. Pools are rank-only by construction, so the
# order half is unreachable end to end — deliberately, for the reason
# `PoolMetadata.sort_order` exists: encoding "pools are rank-only" into the
# reader is what makes a future dispatch change silently wrong. Unreachable
# is not untested, so it is exercised directly here.


def test_the_pool_guard_accepts_an_order_matching_the_pool() -> None:
    reject_pool_sort_mismatch(requested_sort=None, requested_sort_order="desc",
                              pool_sort="rank", pool_sort_order="desc")


def test_the_pool_guard_rejects_an_order_the_pool_was_not_built_with() -> None:
    with pytest.raises(ValidationFailed, match="sort_order"):
        reject_pool_sort_mismatch(requested_sort=None, requested_sort_order="asc",
                                  pool_sort="rank", pool_sort_order="desc")


def test_the_pool_guard_lets_an_unstated_order_pass() -> None:
    """Nothing stated is nothing to contradict — the pool stays the authority."""
    reject_pool_sort_mismatch(requested_sort=None, requested_sort_order=None,
                              pool_sort="rank", pool_sort_order="asc")


def test_the_pool_guard_still_rejects_a_contradicting_sort() -> None:
    """The half that shadows the one above; both must fire on their own."""
    with pytest.raises(ValidationFailed, match="sort"):
        reject_pool_sort_mismatch(requested_sort="date", requested_sort_order=None,
                                  pool_sort="rank", pool_sort_order="desc")
