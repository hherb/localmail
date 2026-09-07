# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A non-finite rerank score must not reach the page's sort key (#361).

``relevance_key``'s first slot is a ``float`` straight from the reranker.
A NaN there makes the comparator **inconsistent** — every comparison
against NaN is ``False``, so neither of a pair is "less than" the other —
and Timsort's result then depends on the order the pool happened to
arrive in. The damage is not confined to the NaN row: rows with perfectly
good scores swap places.

Measured on the shipped key before the fix, with ``A=NaN, B=1.0, C=3.0,
D=2.0``: **9 of the 24** input permutations mis-order the three *finite*
rows, and ``B (1.0)`` outranks ``D (2.0)`` in some of them. No exception,
no log line — the page is simply wrong, and differently wrong each time.

Reachability is narrow and real. It cannot come from RRF: ``k`` and
``rank`` are both ``int``, so every ``1 / (k + rank)`` term is bounded by
1 and a finite sum of them is finite, while a ``k`` that makes ``k + rank``
zero raises ``ZeroDivisionError`` rather than yielding a non-finite value.
The only source is therefore the cross-encoder — which needs
``reranker_enabled = true``, not the default.
``_safe_rerank`` guarded against the reranker *raising* and
``FastEmbedReranker`` against the wrong *length*; neither looked at the
values.

Infinities are refused with NaN even though they order consistently: an
infinite score pins a row to one end of every page it appears on, which
the model cannot have meant, and the remedy is the same one. Splitting
"NaN is bad but inf is fine" would be two predicates for one question.
"""
from __future__ import annotations

import itertools
import logging
import math
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.config import SearchConfig
from localmail.search.query import parse_query
from localmail.search.searcher import FusedHit, Searcher, _safe_rerank

_NAN = float("nan")


class _Reranker:
    """A stub cross-encoder that returns whatever it was handed."""

    name = "fastembed"
    model = "stub/cross-encoder"

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return list(self._scores)


# --------------------------------------------------------------------------
# _safe_rerank: substitute per row, and say so.
# --------------------------------------------------------------------------

def test_a_nan_scored_row_is_ranked_below_every_scored_row() -> None:
    """Per row, not per batch. The whole-batch fallback already exists for
    "the reranker is unusable"; a single bad value must not discard the
    scores the model got right."""
    scores = _safe_rerank(
        _Reranker([0.8, _NAN, 0.2]), "q", ["a", "b", "c"],
        fallback=[0.03, 0.02, 0.01],
    )
    assert scores[0] == 0.8
    assert scores[2] == 0.2
    assert scores[1] < 0.2, scores


def test_the_substitute_is_never_that_row_s_fused_score() -> None:
    """The scale question, on the arrangement that actually occurs.

    RRF is a sum of ``1 / (60 + rank)`` terms — positive, and never above
    ``1/61``. A cross-encoder returns raw logits, routinely negative
    (fastembed's own docstring example is ``[-1.24, -10.6]``). So on a
    query the model rates poorly, splicing a row's fused score into the
    logits puts **the one row it could not score** at the top of the page,
    above three it judged and rejected. Every other fixture in this file
    happens to put the substitute below the finite scores, which is
    exactly why this arrangement needs its own test."""
    logits = [_NAN, -3.5, -6.0, -9.1]
    fused = [0.0164, 0.0328, 0.0161, 0.0159]
    scores = _safe_rerank(_Reranker(logits), "q", ["a", "b", "c", "d"],
                          fallback=fused)
    assert scores[1:] == [-3.5, -6.0, -9.1]
    assert scores[0] < -9.1, scores
    assert scores[0] not in fused, scores


@pytest.mark.parametrize("bad", [_NAN, math.inf, -math.inf])
def test_a_batch_of_nothing_but_non_finite_falls_back_whole(bad: float) -> None:
    """``math.isfinite`` is the predicate, so ±inf goes the same way as NaN.
    With no usable score there is no batch-relative position to demote to,
    so the fused order is the answer — the same one the raise branch gives,
    for the same reason."""
    scores = _safe_rerank(
        _Reranker([bad]), "q", ["a"], fallback=[0.5],
    )
    assert scores == [0.5]


def test_finite_scores_pass_through_untouched() -> None:
    """The positive control for the parametrized test above, whose inputs
    are **all** non-finite — so a rule that substituted unconditionally
    satisfies it. This is what separates "replaces the bad ones" from
    "replaces everything". (Measured: the first test above catches that
    mutation too, because its fallback values differ from its inputs. This
    is the one that catches it by construction rather than by luck of the
    fixture's numbers.)"""
    scores = _safe_rerank(
        _Reranker([0.8, 0.5, 0.2]), "q", ["a", "b", "c"],
        fallback=[0.03, 0.02, 0.01],
    )
    assert scores == [0.8, 0.5, 0.2]


def test_the_substitution_is_logged_with_its_count_and_model(caplog) -> None:
    """Silent degradation is what made this invisible. The count is what
    tells an operator whether one row or the whole pool was affected, and
    the model name is what they act on."""
    with caplog.at_level(logging.WARNING, logger="localmail.search.searcher"):
        _safe_rerank(
            _Reranker([_NAN, 0.5, math.inf]), "q", ["a", "b", "c"],
            fallback=[0.03, 0.02, 0.01],
        )
    assert len(caplog.records) == 1, caplog.records
    # README documents this logger as the grep target. `caplog`'s handler is
    # at the root, so without this the line could move to any logger at all
    # and every other assertion here would still pass.
    assert caplog.records[0].name == "localmail.search.searcher"
    message = caplog.records[0].getMessage()
    assert "returned 2 non-finite score(s) of 3" in message, message
    assert "stub/cross-encoder" in message, message


def test_a_wholly_unusable_batch_is_logged_as_the_fallback_it_is(caplog) -> None:
    """Worded apart from the partial case: "ranked below every row it did
    score" is meaningless when there is no scored row, and the operator's
    page is in fused order rather than mostly-reranked order."""
    with caplog.at_level(logging.WARNING, logger="localmail.search.searcher"):
        _safe_rerank(
            _Reranker([_NAN, math.inf]), "q", ["a", "b"], fallback=[0.02, 0.01],
        )
    assert len(caplog.records) == 1, caplog.records
    message = caplog.records[0].getMessage()
    assert "no usable ordering for this page" in message, message
    assert "falling back to fused RRF scores" in message, message
    # Deliberately no count: `replaced` means "entries substituted", which on
    # this branch is every row and is not the non-finite count on the
    # float-floor path. Quoting it would be a false number on that input.
    assert "non-finite" not in message, message


def test_an_empty_pool_logs_nothing(caplog) -> None:
    """`replaced` and `len(scores)` are equally zero here, so a whole-batch
    check written as the outer condition reports a broken reranker on every
    search that matched nothing."""
    with caplog.at_level(logging.WARNING, logger="localmail.search.searcher"):
        scores = _safe_rerank(_Reranker([]), "q", [], fallback=[])
    assert scores == []
    assert caplog.records == []


def test_an_all_finite_batch_logs_nothing(caplog) -> None:
    """A warning on every search would train an operator to ignore it."""
    with caplog.at_level(logging.WARNING, logger="localmail.search.searcher"):
        _safe_rerank(
            _Reranker([0.8, 0.5]), "q", ["a", "b"], fallback=[0.02, 0.01],
        )
    assert caplog.records == []


# --------------------------------------------------------------------------
# The property the corruption violates, on the page the caller sees.
# --------------------------------------------------------------------------

_WHEN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _row(mid: int) -> dict:
    return {
        "fused": FusedHit(message_id=mid, best_chunk_id=None,
                          best_chunk_table="message", rrf_score=0.5,
                          contributing_arms=[0]),
        "msg": {"account_id": 1, "subject": f"m{mid}", "from_addr": "a@x",
                "from_name": None, "date_sent": _WHEN, "internal_date": None},
        "snippet_source_text": "body text",
    }


def _searcher() -> Searcher:
    """No IO — ``_build_results`` touches the database only to resolve an
    attachment filename, which none of these hits ask for."""
    return Searcher(pool=MagicMock(), cfg=SearchConfig(), embeddings=MagicMock(),
                    reranker=None)


def test_finite_rows_keep_their_order_whatever_order_the_pool_arrives_in() -> None:
    """The headline property, over **all** 24 permutations.

    ``A`` carries the NaN; ``B=1.0``, ``C=3.0``, ``D=2.0`` are sound, so
    the finite rows must read ``C, D, B`` every time. Nine permutations
    failed this before the fix, which is why it is a permutation sweep and
    not one hand-picked input: the two orderings a single input happens to
    produce are as likely to be the correct one as not.
    """
    ids = {"A": 1, "B": 2, "C": 3, "D": 4}
    raw = {"A": _NAN, "B": 1.0, "C": 3.0, "D": 2.0}
    searcher = _searcher()
    for perm in itertools.permutations("ABCD"):
        rows = [_row(ids[n]) for n in perm]
        scores = _safe_rerank(
            _Reranker([raw[n] for n in perm]), "q", list(perm),
            # The fused score is identical for every row, so the NaN row's
            # substitute cannot decide the order of the others by luck.
            fallback=[0.5] * len(perm),
        )
        out = searcher._build_results(rows, parse_query("x"), scores,
                                      page=1, page_size=10)
        page = [r.message_id for r in out]
        finite = [mid for mid in page if mid != ids["A"]]
        assert finite == [ids["C"], ids["D"], ids["B"]], (
            f"input {perm} gave {page}"
        )
        # Free to assert and strictly stronger, but only in one direction:
        # it catches a substitute placed *above* the finite scores
        # (mutation-proven with 99.0, which leaves the filtered assertion
        # above passing in all 24). A substitute at or below them — 0.0,
        # -inf, the shipped `nextafter(min, -inf)` — leaves the NaN row
        # last either way, and is caught by the unit tests, not by this.
        assert page == [ids["C"], ids["D"], ids["B"], ids["A"]], (
            f"input {perm} gave {page}"
        )
