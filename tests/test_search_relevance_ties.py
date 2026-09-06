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

    This asserts *that* there is a tiebreak, not *which*: the direction is
    an **equivalent mutant** here, since both stages downstream re-sort on
    the total ``relevance_key`` and fused order never reaches a caller of
    ``Searcher.search``. Do not add a direction assertion to "close" it —
    it would pin an unobservable, which is what ``relevance_order``'s
    docstring warns against.

    Scope: ``message_id`` only. ``best_chunk_id`` is *still* decided by arm
    order on an exact tie, and that one is observable — see #360.
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


def _row(mid: int, when: datetime | None, *,
         internal: datetime | None = None) -> dict:
    """A hydrated row. ``when`` populates ``date_sent``, ``internal`` the
    ``internal_date`` that outranks it.

    Defaulting ``internal`` to ``None`` is **load-bearing**, and so is
    ``_seed_dated``'s opposite default: between them the two helpers drive
    both branches of ``_msg_date``'s COALESCE, so dropping either column
    from the expression fails somewhere. Consolidating the helpers onto one
    column would silently retire half that coverage. Pass both to test the
    precedence itself.
    """
    return {
        "fused": FusedHit(message_id=mid, best_chunk_id=None,
                          best_chunk_table="message", rrf_score=0.5,
                          contributing_arms=[0]),
        "msg": {"account_id": 1, "subject": f"m{mid}", "from_addr": "a@x",
                "from_name": None, "date_sent": when, "internal_date": internal},
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


def test_the_page_ranks_internal_date_above_date_sent() -> None:
    """The COALESCE argument order on the page, as ``_msg_date`` spells it.

    The columns are crossed, so a swap inverts which row is newer, and the
    winner carries the LOWER id so the ``message_id`` tiebreak cannot
    produce this order by itself.
    """
    rows = [_row(20, _NEW, internal=_OLD), _row(10, _OLD, internal=_NEW)]
    out = _searcher()._build_results(rows, parse_query("x"), [0.5, 0.5],
                                     page=1, page_size=10)
    assert [r.message_id for r in out] == [10, 20]


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


def _seed_dated(conn, dates: list[datetime | None], *,
                sent: list[datetime | None] | None = None) -> list[int]:
    """Seed messages carrying ``internal_date`` from ``dates``.

    ``date_sent`` is left NULL unless ``sent`` is given as a parallel list.
    That default is the mirror of ``_row``'s — see its docstring — so the
    two helpers between them exercise both arguments of the COALESCE that
    ``_fetch_sort_dates`` composes from ``DATE_EXPR_SQL``.
    """
    sent_dates: list[datetime | None] = sent if sent is not None else [None] * len(dates)
    assert len(sent_dates) == len(dates)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = row[0]
        ids = []
        for i, (when, when_sent) in enumerate(zip(dates, sent_dates, strict=True)):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, internal_date, date_sent, headers, raw_bytes, size_bytes)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,'r',1) RETURNING id",
                (acct, f"<t{i}>", bytes([i + 1]) * 32, f"s{i}", "b", when, when_sent),
            )
            got = cur.fetchone()
            assert got is not None
            ids.append(got[0])
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


def test_the_cut_ranks_internal_date_above_date_sent(db_conn):
    """The COALESCE *argument order*, which nothing else here constrains.

    Every other fixture populates exactly one of the two columns, so
    ``COALESCE(internal_date, date_sent)`` and ``COALESCE(date_sent,
    internal_date)`` return the same value and the swap is invisible —
    mutation-proven: swapping both new sites survived the entire suite.

    Here the two columns disagree and are crossed, so the swap inverts
    which row is newer. The winner also carries the LOWER id, so the
    ``message_id`` tiebreak cannot produce this order on its own.
    """
    first_id, second_id = _seed_dated(db_conn, [_NEW, _OLD], sent=[_OLD, _NEW])
    fused = [_fused(second_id, 0.5), _fused(first_id, 0.5)]
    kept = _searcher()._cut_pool(db_conn, fused, limit=1)
    assert [h.message_id for h in kept] == [first_id]


def test_a_pool_hit_missing_from_messages_is_kept_as_undated(db_conn):
    """The documented invariant: a missing id is undated, never dropped.

    ``_fetch_sort_dates``' result is total over its input, so an id absent
    from ``messages`` — reachable when a message is deleted between the
    retrieval arms and the cut, two statements under READ COMMITTED — sorts
    last rather than vanishing. Dropping it would be a silent second cut;
    subscripting a partial map would be a crash on a row the caller
    legitimately holds.

    Two ghosts and one real row against ``limit=2``, so *both* failures are
    observable: discarding the undated rows returns one row where two were
    asked for, and a non-total map raises ``KeyError``. Both mutations were
    run and both fail here.

    Note a third shape — ``[h for h in fused if h.message_id in dates]`` —
    is an **equivalent mutant** and no test can catch it: ``dict.fromkeys``
    seeds every requested id, so that predicate is vacuously true. That is
    the totality doing its job, not a coverage gap; do not "fix" it.
    """
    (real_id,) = _seed_dated(db_conn, [_OLD])
    ghost_lo, ghost_hi = real_id + 1000, real_id + 2000
    fused = [_fused(ghost_lo, 0.5), _fused(real_id, 0.5), _fused(ghost_hi, 0.5)]
    kept = _searcher()._cut_pool(db_conn, fused, limit=2)
    # Real row first (dated beats undated); the ghosts tie on date and fall
    # through to `message_id` descending.
    assert [h.message_id for h in kept] == [real_id, ghost_hi]


def test_the_cut_does_not_reorder_the_caller_s_list(db_conn):
    """``_cut_pool`` takes a ``Sequence`` and returns a fresh list on both
    paths, so neither branch reorders the argument as a side effect."""
    new_id, old_id = _seed_dated(db_conn, [_NEW, _OLD])
    fused = [_fused(old_id, 0.5), _fused(new_id, 0.5)]
    before = list(fused)
    _searcher()._cut_pool(db_conn, fused, limit=1)   # cutting path
    _searcher()._cut_pool(db_conn, fused, limit=9)   # short-circuit path
    assert fused == before


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
    """The real path: fusion, hydration, then the page.

    Not the cut — ``rerank_pool_size`` defaults to 100 and the stubbed arms
    produce two fused hits, so ``_cut_pool`` takes its short-circuit.
    Mutation-proven: reverting the cut to ``fused[:limit]`` does not fail
    this test. Its sibling below sets ``rerank_pool_size=1`` and is the one
    that covers the cut end to end.
    """
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
