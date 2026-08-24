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

from datetime import datetime, timezone

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search_cursor import (
    CursorPlan,
    decode_keyset_cursor,
    encode_keyset_cursor,
    is_keyset_cursor,
    keyset_order,
    resolve_cursor_plan,
)
from localmail.search.searcher import KeysetCursor

_KS = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100)


def test_the_two_directions_mint_different_prefixes() -> None:
    assert encode_keyset_cursor(_KS, "desc").startswith("K|")
    assert encode_keyset_cursor(_KS, "asc").startswith("KA|")


def test_both_prefixes_are_keyset_cursors_and_round_trip() -> None:
    for order in ("asc", "desc"):
        raw = encode_keyset_cursor(_KS, order)
        assert is_keyset_cursor(raw)
        assert keyset_order(raw) == order
        assert decode_keyset_cursor(raw) == _KS


def test_a_legacy_cursor_still_means_descending() -> None:
    """Cursors minted before this change carry no marker and must not flip."""
    legacy = encode_keyset_cursor(_KS, "desc")
    assert keyset_order(legacy) == "desc"
    plan = resolve_cursor_plan(cursor=legacy, requested_sort=None,
                               requested_sort_order=None, free_text="invoice")
    assert plan == CursorPlan(mode="keyset", sort="date", sort_order="desc")


def test_an_ascending_cursor_alone_continues_ascending() -> None:
    """The documented way to page: send the cursor, state nothing else."""
    raw = encode_keyset_cursor(_KS, "asc")
    plan = resolve_cursor_plan(cursor=raw, requested_sort=None,
                               requested_sort_order=None, free_text="invoice")
    assert plan == CursorPlan(mode="keyset", sort="date", sort_order="asc")


def test_a_stated_order_contradicting_the_cursor_is_rejected() -> None:
    raw = encode_keyset_cursor(_KS, "asc")
    with pytest.raises(ValidationFailed, match="sort_order"):
        resolve_cursor_plan(cursor=raw, requested_sort=None,
                            requested_sort_order="desc", free_text="invoice")


def test_a_stated_order_agreeing_with_the_cursor_is_accepted() -> None:
    raw = encode_keyset_cursor(_KS, "asc")
    plan = resolve_cursor_plan(cursor=raw, requested_sort="date",
                               requested_sort_order="asc", free_text="invoice")
    assert plan.sort_order == "asc"


def test_a_blank_query_with_a_keyset_cursor_is_now_allowed() -> None:
    """The blank-query branch honours the cursor since it gained pagination.

    Refusing would forbid exactly the paging that change adds. The cursor
    has never identified a query — it carries a position — so this is the
    same "send the same query and filters" contract that already governs
    every filter.
    """
    raw = encode_keyset_cursor(_KS, "asc")
    plan = resolve_cursor_plan(cursor=raw, requested_sort=None,
                               requested_sort_order=None, free_text="")
    assert plan == CursorPlan(mode="keyset", sort="date", sort_order="asc")


def test_a_fresh_request_resolves_both_defaults() -> None:
    plan = resolve_cursor_plan(cursor=None, requested_sort=None,
                               requested_sort_order=None, free_text="invoice")
    assert plan == CursorPlan(mode="fresh", sort="rank", sort_order="desc")


def test_a_fresh_request_keeps_what_the_caller_stated() -> None:
    plan = resolve_cursor_plan(cursor=None, requested_sort="date",
                               requested_sort_order="asc", free_text="invoice")
    assert plan == CursorPlan(mode="fresh", sort="date", sort_order="asc")


def test_a_pool_cursor_reports_the_pool_mode() -> None:
    plan = resolve_cursor_plan(cursor="tok-1:2", requested_sort=None,
                               requested_sort_order=None, free_text="invoice")
    assert plan.mode == "pool"
