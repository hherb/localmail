# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for the date-keyset SQL rules — pure, no database.

The plan-regression half lives in ``test_searcher_sort_order_plan.py``,
which needs a seeded archive to ask the planner anything. These are the
assertions that need nothing at all: the shape of each emitted fragment,
and the rule deciding whether a page must be topped up from the undated
block (#323).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from localmail.search.date_keyset import (
    DATE_EXPR_SQL,
    DATE_ORDER_BY_SQL,
    UNDATED_TAIL_ONLY_SQL,
    compose_date_keyset_sql,
    keyset_clause,
    needs_undated_top_up,
)
from localmail.search.searcher import KeysetCursor
from localmail.search.sort_axes import SortOrder

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _dated(order: SortOrder) -> KeysetCursor:
    return KeysetCursor(ts=_TS, id=42, order=order)


def _undated(order: SortOrder) -> KeysetCursor:
    return KeysetCursor(ts=None, id=42, order=order)


# ---- The predicate shapes -----------------------------------------------


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_both_dated_predicates_are_row_comparisons(order: SortOrder) -> None:
    """The only spelling Postgres composes into an ``Index Cond`` (#75, #323).

    Asserted for both directions from one test so a future edit cannot
    "simplify" one of them back to the OR-form while the other keeps this
    file green — which is exactly how the descending half came to differ
    from the ascending one in the first place.
    """
    sql, params = keyset_clause(_dated(order), order)
    assert f"ROW({DATE_EXPR_SQL}, m.id)" in sql, sql
    assert " OR " not in sql, (
        "the dated keyset predicate grew a disjunct: Postgres will not "
        f"compose a mixed-column OR into an index range bound\n{sql}"
    )
    assert params == [_TS, 42]


def test_the_descending_dated_predicate_no_longer_admits_undated_rows() -> None:
    """#323's actual change, named rather than implied.

    The ``OR expr IS NULL`` disjunct is what the row comparison replaces,
    and dropping it is what moves those rows onto the top-up query. A test
    for its absence is the one that fails if someone restores it to "fix"
    a short page.
    """
    sql, _ = keyset_clause(_dated("desc"), "desc")
    assert "IS NULL" not in sql, sql


def test_the_two_directions_compare_in_opposite_senses() -> None:
    """Ascending walks forward from the cursor, descending backward."""
    asc_sql, _ = keyset_clause(_dated("asc"), "asc")
    desc_sql, _ = keyset_clause(_dated("desc"), "desc")
    assert ") > ROW(" in asc_sql, asc_sql
    assert ") < ROW(" in desc_sql, desc_sql


@pytest.mark.parametrize(
    ("order", "comparison"), [("asc", "m.id > %s"), ("desc", "m.id < %s")],
)
def test_an_undated_cursor_paginates_within_the_undated_block(
    order: SortOrder, comparison: str,
) -> None:
    """``ts is None`` means the walk is already inside that block.

    Both directions bound on ``id`` there, in their own sense. Neither
    needs the two-phase treatment: ``expr IS NULL`` is a leading-column
    condition the index can bound on, so the ``id`` comparison beside it is
    residual over that block alone rather than over the archive.
    """
    sql, params = keyset_clause(_undated(order), order)
    assert f"{DATE_EXPR_SQL} IS NULL" in sql, sql
    assert comparison in sql, sql
    assert params == [42]


def test_an_unknown_order_is_refused_by_name() -> None:
    """A library caller's wrong literal — the value mypy cannot see.

    It must not fall through into whichever branch happens to be written
    without an ``else``: that pairing once served a walk in the direction
    nobody asked for, stopped only by the ORDER BY lookup that happened to
    run afterwards.
    """
    with pytest.raises(ValueError, match="unknown sort_order 'DESC'"):
        keyset_clause(_dated("desc"), "DESC")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown sort_order 'DESC'"):
        compose_date_keyset_sql(where="TRUE", order="DESC")  # type: ignore[arg-type]


# ---- The emitter --------------------------------------------------------


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_the_emitter_carries_the_directions_own_order_by(order: SortOrder) -> None:
    """One SQL shape, parameterised by direction — never a second copy.

    Both phases of a descending page go through this, which is what stops
    the top-up query drifting from the cursor query the way a hand-written
    second statement would.
    """
    sql = compose_date_keyset_sql(where="TRUE AND x", order=order)
    assert DATE_ORDER_BY_SQL[order] in sql, sql
    assert "TRUE AND x" in sql, sql
    assert sql.rstrip().endswith("LIMIT %s"), sql


def test_the_top_up_predicate_carries_no_parameters() -> None:
    """What makes it safe to substitute for a cursor predicate.

    ``api.browse.build_where`` needs a runtime ``raise`` to stop a cursor
    being passed alongside its ``null_tail_only`` mode. Here the top-up is
    a bare constant used *instead of* the cursor fragment, so the same
    invariant holds by construction — but only while it takes no params.
    """
    assert "%s" not in UNDATED_TAIL_ONLY_SQL
    assert f"{DATE_EXPR_SQL} IS NULL" in UNDATED_TAIL_ONLY_SQL


# ---- The top-up rule ----------------------------------------------------


def test_a_short_descending_dated_page_is_topped_up() -> None:
    """The one shape that needs it."""
    assert needs_undated_top_up(
        keyset=_dated("desc"), order="desc", rows_returned=3, fetch_limit=51,
    )


def test_a_full_descending_page_is_not_topped_up() -> None:
    """No slots to fill; the next page's cursor reaches the tail in turn."""
    assert not needs_undated_top_up(
        keyset=_dated("desc"), order="desc", rows_returned=51, fetch_limit=51,
    )


def test_page_one_is_never_topped_up() -> None:
    """With no cursor predicate the index walk streams into the tail itself.

    Topping up here would double-count: those rows are already reachable
    by the same statement.
    """
    assert not needs_undated_top_up(
        keyset=None, order="desc", rows_returned=0, fetch_limit=51,
    )


def test_a_cursor_already_inside_the_undated_block_is_not_topped_up() -> None:
    """It is being paginated by id within that block.

    A top-up carries no ``id`` bound, so it would re-emit rows the caller
    has already been given — the failure mode is duplicates, not omissions,
    which is why it needs its own assertion rather than riding on the
    others.
    """
    assert not needs_undated_top_up(
        keyset=_undated("desc"), order="desc", rows_returned=1, fetch_limit=51,
    )


def test_ascending_is_never_topped_up() -> None:
    """Ascending meets the undated block at the *head* of its walk.

    ``keyset_clause``'s own ascending predicates already reach it, in both
    the dated and the undated-cursor case. A top-up would append that block
    to the end of an ascending page, which is the wrong end entirely.
    """
    assert not needs_undated_top_up(
        keyset=_dated("asc"), order="asc", rows_returned=3, fetch_limit=51,
    )
