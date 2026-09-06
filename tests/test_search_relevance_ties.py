# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Equally relevant rows are ordered newest first.

``sort="date"`` has always been strict — the SQL is
``ORDER BY COALESCE(internal_date, date_sent) DESC NULLS LAST, m.id DESC``.
The rank path had no such rule: ``_build_results`` sorted on the score
alone, and Python's sort is stable, so equal scores kept whatever order
fusion produced. That order is the sequence the four retrieval arms
happened to be walked in, which carries no date information at all.

Ties are ordinary here, not exotic. An RRF score is a sum of
``1 / (k + rank)``, so any two messages hit at the same rank in different
arms score identically — and with the reranker off (the default) the RRF
score *is* the final score.

Three places have to agree, or the rule holds on one page and not the
next: fusion's own ordering, the cut to ``rerank_pool_size`` (which
decides *which* equally relevant rows survive at all), and the assembly
of a page. They share one key, ``relevance_order.relevance_key``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from unittest.mock import MagicMock, patch

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.query import parse_query
from localmail.search.searcher import ArmHit, FusedHit, Searcher, rrf_fuse


def _hit(mid: int, rank: int) -> ArmHit:
    return ArmHit(message_id=mid, chunk_id=None, chunk_table="message",
                  arm_score=1.0, rank=rank)


def test_fusion_breaks_a_tie_deterministically_not_by_arm_order() -> None:
    """Two messages at the same rank in different arms score identically.

    The order used to come from ``agg`` insertion order — i.e. which arm
    was walked first — so passing the same two arms the other way round
    silently reversed the result.
    """
    a, b = [_hit(10, rank=3)], [_hit(20, rank=3)]
    forward = [h.message_id for h in rrf_fuse([a, b], k=60)]
    backward = [h.message_id for h in rrf_fuse([b, a], k=60)]
    assert forward == backward, "arm order must not decide a tie"


def test_fusion_still_orders_by_score_first() -> None:
    """The positive control: a tiebreak that outranked the score would
    reorder genuinely different relevances."""
    a = [_hit(10, rank=1), _hit(20, rank=9)]
    out = rrf_fuse([a], k=60)
    assert [h.message_id for h in out] == [10, 20]


# --------------------------------------------------------------------------
# The page: equal score, newer first.
# --------------------------------------------------------------------------

def _searcher() -> Searcher:
    """No IO: ``__init__`` only stores, and ``_build_results`` touches the
    database solely to resolve an attachment filename, which none of these
    hits ask for."""
    return Searcher(pool=MagicMock(), cfg=SearchConfig(), embeddings=MagicMock(),
                    reranker=None)


def _row(mid: int, when: datetime | None) -> dict:
    return {
        "fused": FusedHit(message_id=mid, best_chunk_id=None,
                          best_chunk_table="message", rrf_score=0.5,
                          contributing_arms=[0]),
        "msg": {"account_id": 1, "subject": f"m{mid}", "from_addr": "a@x",
                "from_name": None, "date_sent": when, "internal_date": None},
        "snippet_source_text": "body text",
    }


_OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)
_NEW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_equally_relevant_rows_are_ordered_newest_first() -> None:
    """The rule, on the page the caller actually sees.

    The newer message carries the **lower** id deliberately. Ids ascend with
    insertion, so a fixture seeded oldest-first makes date order and id order
    agree — and every assertion here would then also pass for a tiebreak that
    read only ``message_id`` and no date at all. Mutation-proven: dropping
    the real dates survives the agreeing fixture and fails this one.
    """
    rows = [_row(20, _OLD), _row(10, _NEW)]
    out = _searcher()._build_results(rows, parse_query("x"), [0.5, 0.5],
                                     page=1, page_size=10)
    assert [r.message_id for r in out] == [10, 20]


def test_a_higher_score_still_outranks_a_newer_date() -> None:
    """The positive control: date is the *tiebreak*, not the sort. A key that
    put the date first would reorder genuinely different relevances."""
    rows = [_row(20, _OLD), _row(10, _NEW)]
    out = _searcher()._build_results(rows, parse_query("x"), [0.9, 0.1],
                                     page=1, page_size=10)
    assert [r.message_id for r in out] == [20, 10]


def test_an_undated_row_sorts_after_a_dated_one_of_equal_relevance() -> None:
    """``NULLS LAST``, matching the date walk's own SQL. Both date columns
    are nullable and archive imports really do produce such rows."""
    rows = [_row(20, None), _row(10, _OLD)]
    out = _searcher()._build_results(rows, parse_query("x"), [0.5, 0.5],
                                     page=1, page_size=10)
    assert [r.message_id for r in out] == [10, 20]


def test_rows_equal_on_score_and_date_still_order_deterministically() -> None:
    """Bulk sends share a timestamp to the second, so the date tiebreak has
    its own ties. Without a final key the order is input order, which is the
    arbitrary thing this file exists to remove."""
    forward = _searcher()._build_results([_row(10, _OLD), _row(20, _OLD)],
                                         parse_query("x"), [0.5, 0.5],
                                         page=1, page_size=10)
    backward = _searcher()._build_results([_row(20, _OLD), _row(10, _OLD)],
                                          parse_query("x"), [0.5, 0.5],
                                          page=1, page_size=10)
    assert [r.message_id for r in forward] == [r.message_id for r in backward]


# --------------------------------------------------------------------------
# The cut: which equally relevant rows survive to be hydrated at all.
# --------------------------------------------------------------------------

def _fused(mid: int, score: float) -> FusedHit:
    return FusedHit(message_id=mid, best_chunk_id=None,
                    best_chunk_table="message", rrf_score=score,
                    contributing_arms=[0])


def _seed_dated(conn, dates: list[datetime | None]) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        ids = []
        for i, when in enumerate(dates):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, internal_date, headers, raw_bytes, size_bytes)"
                " VALUES (%s,%s,%s,%s,%s,%s,'{}'::jsonb,'r',1) RETURNING id",
                (acct, f"<t{i}>", bytes([i + 1]) * 32, f"s{i}", "b", when),
            )
            ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


def test_the_pool_cut_keeps_the_newer_of_two_equally_relevant_rows(db_conn):
    """The cut runs before hydration, so a page-level rule alone would order
    exactly and still have dropped the newer message on the way in."""
    # Newest seeded FIRST, so it carries the lower id and date order
    # contradicts id order. Seeded the other way this passes for a cut that
    # never looks up a date — mutation-proven.
    new_id, old_id = _seed_dated(db_conn, [_NEW, _OLD])
    fused = [_fused(old_id, 0.5), _fused(new_id, 0.5)]
    kept = _searcher()._cut_pool(db_conn, fused, limit=1)
    assert [h.message_id for h in kept] == [new_id]


def test_the_pool_cut_still_prefers_a_higher_score(db_conn):
    """Positive control: the date must not outrank relevance at the cut
    either, or the pool becomes a date walk wearing a rank label."""
    new_id, old_id = _seed_dated(db_conn, [_NEW, _OLD])
    fused = [_fused(new_id, 0.1), _fused(old_id, 0.9)]
    kept = _searcher()._cut_pool(db_conn, fused, limit=1)
    assert [h.message_id for h in kept] == [old_id]


def test_the_pool_cut_skips_the_date_query_when_nothing_is_dropped(db_conn):
    """A pool that already fits is returned untouched, so the common case
    pays no extra round trip."""
    ids = _seed_dated(db_conn, [_OLD, _NEW])
    fused = [_fused(ids[0], 0.5), _fused(ids[1], 0.5)]
    with patch.object(Searcher, "_fetch_sort_dates") as spy:
        kept = _searcher()._cut_pool(db_conn, fused, limit=5)
    spy.assert_not_called()
    assert [h.message_id for h in kept] == [ids[0], ids[1]]


def test_an_undated_row_loses_the_cut_to_a_dated_one(db_conn):
    """``NULLS LAST`` at the cut too, matching the date walk."""
    # Dated row seeded first, so it has the lower id: the undated row would
    # win on `message_id` alone, and loses only because it is undated.
    old_id, none_id = _seed_dated(db_conn, [_OLD, None])
    fused = [_fused(none_id, 0.5), _fused(old_id, 0.5)]
    kept = _searcher()._cut_pool(db_conn, fused, limit=1)
    assert [h.message_id for h in kept] == [old_id]


# --------------------------------------------------------------------------
# End to end, through the real `Searcher.search`.
# --------------------------------------------------------------------------
#
# The arms are stubbed rather than driven by real SQL, and that is what makes
# this deterministic: a genuine tie needs two messages hit at the same rank in
# different arms, which no seeded corpus produces reliably — identical content
# gives `ts_rank_cd` identical scores and Postgres then breaks *that* tie
# arbitrarily, so the arm ranks themselves would vary between runs. Stubbing
# the arms fixes the tie and leaves fusion, the cut, hydration and page
# assembly as the real code under test.

class _Embeddings:
    name = "stub"; model = "stub"; dimension = 768
    def embed_documents(self, texts): return [[1.0] * 768 for _ in texts]
    def embed_query(self, text): return [0.5] * 768
    def health_check(self): pass


def _tied_arms(first: int, second: int):
    """Two messages at the same rank in different arms — identical RRF."""
    def a1(conn, parsed, cfg, limit):
        return [ArmHit(message_id=first, chunk_id=None, chunk_table="message",
                       arm_score=1.0, rank=1)]
    def a2(conn, parsed, cfg, limit):
        return [ArmHit(message_id=second, chunk_id=None, chunk_table="message",
                       arm_score=1.0, rank=1)]
    def empty(conn, parsed, cfg, *a, **kw):
        return []
    return a1, a2, empty


def _live_searcher(db_dsn: str, **cfg_kw) -> Searcher:
    return Searcher(pool=open_pool(db_dsn, min_size=1, max_size=2),
                    cfg=SearchConfig(reranker_enabled=False, **cfg_kw),
                    embeddings=_Embeddings(), reranker=None)


def test_search_returns_equally_relevant_rows_newest_first(db_dsn, db_conn):
    """The whole path: fusion, the cut, then the page."""
    new_id, old_id = _seed_dated(db_conn, [_NEW, _OLD])
    a1, a2, empty = _tied_arms(old_id, new_id)
    searcher = _live_searcher(db_dsn)
    try:
        with patch("localmail.search.arms.arm_bm25_messages", a1), \
             patch("localmail.search.arms.arm_bm25_chunks", a2), \
             patch("localmail.search.arms.arm_vector_chunks", empty), \
             patch("localmail.search.arms.arm_vector_attachment_chunks", empty):
            page = searcher.search("needle", allowed_account_ids=None,
                                   user_id=1, page_size=10)
    finally:
        searcher._pool.close()
    assert [r.message_id for r in page.results] == [new_id, old_id]


def test_search_keeps_the_newer_row_when_the_pool_is_cut(db_dsn, db_conn):
    """`rerank_pool_size=1` forces the cut, so the older row never reaches
    hydration. Ordering the page alone would be exact about a row that had
    already been discarded."""
    new_id, old_id = _seed_dated(db_conn, [_NEW, _OLD])
    a1, a2, empty = _tied_arms(old_id, new_id)
    searcher = _live_searcher(db_dsn, rerank_pool_size=1)
    try:
        with patch("localmail.search.arms.arm_bm25_messages", a1), \
             patch("localmail.search.arms.arm_bm25_chunks", a2), \
             patch("localmail.search.arms.arm_vector_chunks", empty), \
             patch("localmail.search.arms.arm_vector_attachment_chunks", empty):
            page = searcher.search("needle", allowed_account_ids=None,
                                   user_id=1, page_size=10)
    finally:
        searcher._pool.close()
    assert [r.message_id for r in page.results] == [new_id]
