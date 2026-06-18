# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for the pure RRF fusion function."""

from __future__ import annotations

from localmail.search.searcher import ArmHit, rrf_fuse


def _hit(mid, cid, table, rank, score=0.0):
    return ArmHit(message_id=mid, chunk_id=cid, chunk_table=table,
                  arm_score=score, rank=rank)


def test_rrf_single_arm_orders_by_rank():
    arm = [_hit(10, 1, "message_chunks", rank=1),
           _hit(20, 2, "message_chunks", rank=2),
           _hit(30, 3, "message_chunks", rank=3)]
    out = rrf_fuse([arm], k=60)
    assert [h.message_id for h in out] == [10, 20, 30]


def test_rrf_two_arms_sum_contributions():
    a = [_hit(10, 1, "message_chunks", rank=1),
         _hit(20, 2, "message_chunks", rank=3)]
    b = [_hit(20, 4, "message_chunks", rank=1),
         _hit(10, 5, "message_chunks", rank=4)]
    out = rrf_fuse([a, b], k=60)
    # Message 20: 1/(60+3) + 1/(60+1) = 0.0322  | Message 10: 1/(60+1) + 1/(60+4) = 0.0320
    assert [h.message_id for h in out] == [20, 10]


def test_rrf_dedupes_to_one_chunk_per_message():
    a = [_hit(10, 1, "message_chunks", rank=1),
         _hit(10, 2, "message_chunks", rank=5)]
    out = rrf_fuse([a], k=60)
    assert len(out) == 1
    # winner chunk = the one with the largest single contribution
    assert out[0].best_chunk_id == 1


def test_rrf_records_contributing_arms():
    a = [_hit(10, 1, "message_chunks", rank=1)]
    b = [_hit(10, 2, "message_chunks", rank=2)]
    c = []  # arm with no hits
    out = rrf_fuse([a, b, c], k=60)
    assert out[0].contributing_arms == [0, 1]


def test_rrf_empty_input():
    assert rrf_fuse([], k=60) == []
    assert rrf_fuse([[], []], k=60) == []
