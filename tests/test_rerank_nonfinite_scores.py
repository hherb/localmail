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

Reachability is narrow and real. It cannot come from RRF (a sum of
``1 / (k + rank)`` positives; a pathological ``rrf_k`` raises
``ZeroDivisionError`` rather than yielding NaN), so the only source is the
cross-encoder — which needs ``reranker_enabled = true``, not the default.
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

def test_a_nan_score_is_replaced_by_that_row_s_fused_score() -> None:
    """Per row, not per batch. The whole-batch fallback already exists for
    "the reranker is unusable"; a single bad value must not discard the
    scores the model got right."""
    scores = _safe_rerank(
        _Reranker([0.8, _NAN, 0.2]), "q", ["a", "b", "c"],
        fallback=[0.03, 0.02, 0.01],
    )
    assert scores == [0.8, 0.02, 0.2]


@pytest.mark.parametrize("bad", [_NAN, math.inf, -math.inf])
def test_every_non_finite_score_is_replaced(bad: float) -> None:
    """``math.isfinite`` is the predicate, so ±inf goes the same way as NaN."""
    scores = _safe_rerank(
        _Reranker([bad]), "q", ["a"], fallback=[0.5],
    )
    assert scores == [0.5]


def test_finite_scores_pass_through_untouched() -> None:
    """The positive control: a rule that replaced everything would satisfy
    every assertion above."""
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
    message = caplog.records[0].getMessage()
    assert "returned 2 non-finite score(s) of 3" in message, message
    assert "stub/cross-encoder" in message, message


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
        # The NaN row lands where its substituted fallback (0.5) puts it,
        # below every finite score here. Free to assert and strictly
        # stronger, so a substitution that used some other value — 0.0,
        # -inf — would fail here as well as in the unit tests above.
        assert page == [ids["C"], ids["D"], ids["B"], ids["A"]], (
            f"input {perm} gave {page}"
        )
