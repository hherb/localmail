# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for the relevance path's ordering rule — pure, no database.

The behavioural half lives in ``test_search_relevance_ties.py``, which
drives the rule through ``_cut_pool`` and ``_build_results`` against a
seeded archive. These are the assertions that need nothing at all: the
shape of the key, the precedence of its three components, and the one
cross-boundary claim the module makes — that its Python NULLS-LAST rule
says the same thing as the date walk's SQL.

Every sibling pure module in ``search/`` has a file like this
(``test_date_keyset.py``, ``test_sort_axes.py``, ``test_text_empty.py``).
Without it the only pins on a two-line pure function were six tests that
each cost a Postgres round trip.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from localmail.search.date_keyset import DATE_ORDER_BY_SQL
from localmail.search.relevance_order import (
    DateKey,
    FiniteScores,
    date_key,
    finite_scores,
    relevance_key,
)

_OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)
_NEW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# date_key: NULLS LAST under reverse=True.
# --------------------------------------------------------------------------

def test_a_dated_row_outranks_an_undated_one() -> None:
    """Ascending, undated sorts first — so reversed it lands last, which is
    what ``DESC NULLS LAST`` means."""
    assert date_key(None) < date_key(_OLD)


def test_dated_rows_order_by_their_timestamp() -> None:
    assert date_key(_OLD) < date_key(_NEW)


def test_two_undated_rows_compare_equal() -> None:
    """So the decision passes to the next component of ``relevance_key``
    rather than being made arbitrarily here."""
    assert date_key(None) == date_key(None)


def test_the_key_is_a_flag_then_a_datetime() -> None:
    """The flag is what places an undated row, not the padding beside it.

    Slot 0 is the whole mechanism: ``(0, …) < (1, …)`` decides before the
    second slot is ever read. Pinning the shape is what stops someone
    "simplifying" this to a bare ``when or SENTINEL``, which *is*
    value-dependent and does break on a naive/aware mix.
    """
    assert date_key(_OLD) == (1, _OLD)
    flag, padding = date_key(None)
    assert flag == 0
    assert isinstance(padding, datetime)


def test_the_padding_value_is_never_compared() -> None:
    """The sentinel is padding, not an ordering device — so a value that
    would sort *after* every real timestamp must change nothing.

    Substituting a datetime that raises on every comparison operator would
    be the stronger form; this is the cheap version of the same claim, and
    it fails immediately if the flag is ever dropped from the key.
    """
    import localmail.search.relevance_order as mod

    original = mod._NULL_DATE_SENTINEL
    rows = [_NEW, None, _OLD, None]
    expected = sorted(rows, key=date_key, reverse=True)
    try:
        mod._NULL_DATE_SENTINEL = datetime.max.replace(tzinfo=timezone.utc)
        assert sorted(rows, key=date_key, reverse=True) == expected
    finally:
        mod._NULL_DATE_SENTINEL = original


# --------------------------------------------------------------------------
# The differential: the Python rule and the SQL rule say the same thing.
# --------------------------------------------------------------------------

def test_the_python_nulls_rule_matches_the_date_walk_s_sql() -> None:
    """``date_key`` and ``DATE_ORDER_BY_SQL`` are independent statements of
    one rule, so nothing but a test stops them drifting.

    The live date walk orders in SQL and never calls ``date_key``; the rank
    tiebreak calls ``date_key`` and never sees the SQL. They agree today by
    parallel wording. This is the ``ALLOWLISTED_WHERE_SQL`` /
    ``is_allowlisted`` arrangement — a differential test over the same
    inputs, which is what makes "cannot disagree" true rather than asserted.
    """
    assert "DESC NULLS LAST" in DATE_ORDER_BY_SQL["desc"]
    # NULLS LAST descending == undated sorting lowest ascending.
    assert date_key(None) < date_key(_OLD)

    assert "ASC NULLS FIRST" in DATE_ORDER_BY_SQL["asc"]
    # NULLS FIRST ascending == the same relation, unreversed.
    assert date_key(None) < date_key(_OLD)


def test_the_id_tiebreak_runs_the_same_way_as_the_walk_s() -> None:
    """The walk ends ``m.id DESC``; ``relevance_key`` ends ``message_id``
    under ``reverse=True``. Same direction — higher id first on a tie."""
    assert DATE_ORDER_BY_SQL["desc"].endswith("m.id DESC")
    tied = [(_OLD, 10), (_OLD, 20)]
    ordered = sorted(
        tied,
        key=lambda r: relevance_key(score=0.5, when=r[0], message_id=r[1]),
        reverse=True,
    )
    assert [mid for _, mid in ordered] == [20, 10]


# --------------------------------------------------------------------------
# relevance_key: score, then date, then id — in that order.
# --------------------------------------------------------------------------

def _order(rows: list[tuple[float, datetime | None, int]]) -> list[int]:
    return [
        mid for _, _, mid in sorted(
            rows,
            key=lambda r: relevance_key(score=r[0], when=r[1], message_id=r[2]),
            reverse=True,
        )
    ]


def test_score_outranks_date() -> None:
    """The positive control. A key leading with the date would turn every
    relevance search into a date search — the opposite failure, and much
    harder to notice than no tiebreak at all."""
    assert _order([(0.9, _OLD, 10), (0.1, _NEW, 20)]) == [10, 20]


def test_equal_scores_order_newest_first() -> None:
    """The rule itself. The newer message carries the LOWER id, so this
    cannot pass for a key that reads only ``message_id``."""
    assert _order([(0.5, _OLD, 20), (0.5, _NEW, 10)]) == [10, 20]


def test_date_outranks_id() -> None:
    """The date is a real tiebreak, not decoration beside the id."""
    assert _order([(0.5, _OLD, 99), (0.5, _NEW, 1)]) == [1, 99]


def test_equal_score_and_date_fall_through_to_the_id() -> None:
    """Bulk sends share ``date_sent`` to the second, so the date tiebreak
    has ties of its own. Without this component the remainder is input
    order — the arbitrary thing the module exists to remove."""
    assert _order([(0.5, _OLD, 10), (0.5, _OLD, 20)]) == [20, 10]


def test_an_undated_row_loses_to_a_dated_one_of_equal_relevance() -> None:
    """NULLS LAST, reached through ``relevance_key`` rather than directly.

    The undated row carries the higher id, so it would win on the final
    component alone and loses only because it is undated.
    """
    assert _order([(0.5, None, 20), (0.5, _OLD, 10)]) == [10, 20]


def test_the_key_is_a_total_order_over_a_fused_pool() -> None:
    """``rrf_fuse`` emits one hit per ``message_id``, so no two entries can
    tie on all three components — which is why the result is independent of
    input order, and why the fusion-level tiebreak is defence in depth
    rather than a stage the page depends on.
    """
    rows = [(0.5, _OLD, 10), (0.5, _OLD, 20), (0.5, None, 30), (0.9, _NEW, 40)]
    assert _order(rows) == _order(list(reversed(rows)))
    assert _order(rows) == _order([rows[2], rows[0], rows[3], rows[1]])


def test_the_key_has_three_components_in_the_documented_order() -> None:
    """The shape is the contract the two call sites rely on positionally."""
    score, dk, mid = relevance_key(score=0.25, when=_OLD, message_id=7)
    assert score == 0.25
    assert dk == date_key(_OLD)
    assert mid == 7


def test_date_key_annotation_is_the_published_alias() -> None:
    """``DateKey`` is the module's own name for slot 1's type; it is what
    ``relevance_key``'s return annotation is written in terms of."""
    assert DateKey == tuple[int, datetime]


# --------------------------------------------------------------------------
# finite_scores: slot 0's totality, the counterpart of date_key's flag.
# --------------------------------------------------------------------------

def test_a_non_finite_score_is_replaced_by_its_fallback() -> None:
    """Positional, so the surviving scores stay attached to their own rows."""
    out = finite_scores([0.8, float("nan"), 0.2], fallback=[0.03, 0.02, 0.01])
    assert out.scores == [0.8, 0.02, 0.2]


def test_both_infinities_are_replaced_as_well() -> None:
    """``math.isfinite`` is the predicate. An infinite score orders
    *consistently* — unlike NaN it does not corrupt its neighbours — but it
    pins its row to one end of every page, which no model can have meant,
    and the remedy is the fallback either way. Two predicates for one
    question is the drift this module exists to avoid."""
    out = finite_scores([math.inf, -math.inf], fallback=[0.02, 0.01])
    assert out.scores == [0.02, 0.01]


def test_an_all_finite_list_is_returned_unchanged() -> None:
    """The positive control: a rule that substituted unconditionally would
    satisfy both assertions above."""
    out = finite_scores([0.8, 0.5, 0.2], fallback=[0.03, 0.02, 0.01])
    assert out.scores == [0.8, 0.5, 0.2]
    assert out.replaced == 0


def test_the_count_is_how_many_were_replaced() -> None:
    """The caller logs it, and a second count taken at the call site would
    be a second reading of the same rule."""
    out = finite_scores([float("nan"), 0.5, math.inf], fallback=[0.03, 0.02, 0.01])
    assert out.replaced == 2


def test_the_result_is_a_fresh_list_on_both_paths() -> None:
    """One signature must not have two aliasing contracts — the trap
    ``_cut_pool``'s own note records. The all-finite path is the one that
    would be tempting to short-circuit by returning the argument."""
    scores = [0.8, 0.5]
    assert finite_scores(scores, fallback=[0.0, 0.0]).scores is not scores
    bad = [float("nan"), 0.5]
    assert finite_scores(bad, fallback=[0.0, 0.0]).scores is not bad


def test_a_length_mismatch_raises_rather_than_truncating() -> None:
    """A silent truncation would drop rows off the page. ``zip`` without
    ``strict`` does exactly that, so the flag is load-bearing."""
    with pytest.raises(ValueError):
        finite_scores([0.8, 0.5, 0.2], fallback=[0.03, 0.02])


def test_the_result_is_read_by_field_and_is_not_iterable() -> None:
    """``FiniteScores`` is a dataclass rather than a ``NamedTuple``, and the
    difference is a silent failure.

    A caller that forgets ``.scores`` binds the container itself. As a
    two-field ``NamedTuple`` that is an iterable of length 2, so
    ``_build_results``' ``zip(hydrated, scores, strict=True)`` **accepts
    it** whenever the pool happens to hold two rows — a page ordered by a
    list and an int. A dataclass is not iterable, so the same slip is a
    ``TypeError`` at the first zip. Loud beats lucky.
    """
    out = finite_scores([float("nan")], fallback=[0.5])
    assert isinstance(out, FiniteScores)
    assert out.scores == [0.5]
    assert out.replaced == 1
    with pytest.raises(TypeError):
        list(out)
