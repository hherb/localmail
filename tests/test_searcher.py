# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""End-to-end Searcher tests against real Postgres with stubbed backends."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import SearchPage, Searcher


class _Embedder:
    name = "stub"; model = "stub"; dimension = 768

    def embed_documents(self, texts):
        return [[1.0 / (i + 1)] * 768 for i, _ in enumerate(texts)]

    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


class _Reranker:
    name = "stub"; model = "stub"

    def rerank(self, query, candidates):
        # Prefer candidates containing the query verbatim
        return [1.0 if query.lower() in c.lower() else 0.5 for c in candidates]


def _seed(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i, (s, b) in enumerate([
            ("Berlin conference next week", "Looking forward to Berlin"),
            ("Lunch tomorrow", "Want to grab lunch?"),
            ("Conference review", "How was the conference?"),
        ]):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, s, b),
            )
    conn.commit()


def test_searcher_returns_results(db_dsn, db_conn):
    _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=_Reranker(), rewriter=None)
        page = s.search("Berlin", allowed_account_ids=None)
    finally:
        pool.close()
    assert isinstance(page, SearchPage)
    assert page.page == 1
    assert page.results, "expected at least one result"
    assert any("Berlin" in r.subject for r in page.results)
    assert page.search_token


def test_searcher_timing_ms_populated(db_dsn, db_conn):
    _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=_Reranker(), rewriter=None)
        page = s.search("Berlin", allowed_account_ids=None)
    finally:
        pool.close()
    assert "parse" in page.timing_ms
    assert "retrieve" in page.timing_ms
    assert "rerank" in page.timing_ms
    assert "total" in page.timing_ms


def _seed_with_dates(conn, rows):
    """Seed messages with explicit (account_name, subject, date_sent, internal_date).

    Returns the list of inserted message IDs in seed order.
    """
    ids = []
    accounts: dict[str, int] = {}
    with conn.cursor() as cur:
        for i, (acct_name, subject, date_sent, internal_date) in enumerate(rows):
            if acct_name not in accounts:
                cur.execute(
                    "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                    " VALUES (%s, %s, 'h', 'password') RETURNING id",
                    (acct_name, f"{acct_name}@x"),
                )
                accounts[acct_name] = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, date_sent, internal_date)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1, %s, %s) RETURNING id",
                (accounts[acct_name], f"<m{i}>", bytes([i + 1]) * 32, subject, "body",
                 date_sent, internal_date),
            )
            ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


def test_searcher_empty_query_returns_messages_by_coalesce_internal_date_date_sent_desc(
    db_dsn, db_conn,
):
    """An empty free-text query is the canonical "show me my mail" default.

    The hybrid pipeline degenerates badly for empty queries: BM25 arms
    return [] (no terms to match), and vector arms rank by cosine distance
    to the embedding of the empty string — producing exactly
    `rerank_pool_size` (default 20) arbitrary-looking hits. The Searcher
    must detect this and fall back to a date-ordered list.

    Ordering is ``COALESCE(internal_date, date_sent) DESC NULLS LAST,
    id DESC``: rows backfilled to a real IMAP INTERNALDATE sort by it;
    legacy rows fall through to the email header date. The seed mixes
    both states.
    """
    now = datetime.now(timezone.utc)
    mid_a, mid_b, mid_c = _seed_with_dates(db_conn, [
        # internal_date populated, oldest of the three.
        ("a", "old backfilled archive", now - timedelta(days=365), now - timedelta(days=30)),
        # internal_date NULL, fall through to date_sent — newest signal.
        ("a", "fresh, not yet backfilled", now - timedelta(hours=1), None),
        # internal_date populated, middle position.
        ("a", "middle by INTERNALDATE", now - timedelta(days=10), now - timedelta(days=2)),
    ])
    cfg = SearchConfig()
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=_Reranker(), rewriter=None)
        page = s.search("", allowed_account_ids=None)
    finally:
        pool.close()
    ids = [r.message_id for r in page.results]
    assert ids == [mid_b, mid_c, mid_a]


def test_searcher_sort_date_orders_results_by_internal_date_desc(db_dsn, db_conn):
    """`sort="date"` re-orders the hybrid result page by
    ``COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC`` while
    still drawing candidates from the same hybrid retrieval pool — i.e.
    "find relevant matches, then show them newest first" rather than
    "by relevance".

    The seed gives every message the same query term so all three are
    eligible; without the sort flag the reranker would pick a different
    order. The flag must override that.
    """
    now = datetime.now(timezone.utc)
    mid_oldest, mid_middle, mid_newest = _seed_with_dates(db_conn, [
        ("a", "berlin lunch 2022", now - timedelta(days=365), now - timedelta(days=365)),
        ("a", "berlin lunch 2024", now - timedelta(days=60), now - timedelta(days=60)),
        ("a", "berlin lunch today", now - timedelta(hours=1), now - timedelta(hours=1)),
    ])
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=_Reranker(), rewriter=None)
        page = s.search("berlin", allowed_account_ids=None, sort="date")
    finally:
        pool.close()
    ids = [r.message_id for r in page.results]
    assert ids == [mid_newest, mid_middle, mid_oldest]


def test_searcher_sort_rank_is_default_and_uses_rerank_score(db_dsn, db_conn):
    """Sanity: the default `sort="rank"` keeps the existing behavior of
    ordering by rerank score, even when dates would have produced a
    different order. Pins the rerank path against accidental re-sort
    regressions when `sort` lands.
    """
    now = datetime.now(timezone.utc)
    # All same date_sent / internal_date so date-sort would be ambiguous.
    same_when = now - timedelta(hours=1)
    mid_a, mid_b = _seed_with_dates(db_conn, [
        ("a", "irrelevant subject", same_when, same_when),
        ("a", "berlin conference", same_when, same_when),
    ])
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=_Reranker(), rewriter=None)
        # _Reranker prefers candidates with the query verbatim → mid_b wins.
        page = s.search("berlin", allowed_account_ids=None)
    finally:
        pool.close()
    ids = [r.message_id for r in page.results]
    assert ids[0] == mid_b


def test_searcher_empty_query_respects_account_filter(db_dsn, db_conn):
    """The empty-query fallback must still honor structured filters — an
    account scope clicked in the GUI tree should narrow the recent-mail
    list, not be discarded along with the (absent) free-text query.
    """
    now = datetime.now(timezone.utc)
    mid_a1, mid_b1, mid_a2 = _seed_with_dates(db_conn, [
        ("acct_a", "a-1", now - timedelta(days=5), now - timedelta(days=5)),
        ("acct_b", "b-1", now - timedelta(days=3), now - timedelta(days=3)),
        ("acct_a", "a-2", now - timedelta(days=1), now - timedelta(days=1)),
    ])
    # Resolve acct_a's id for the filter.
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE name = 'acct_a'")
        acct_a_id = cur.fetchone()[0]
    cfg = SearchConfig()
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=_Reranker(), rewriter=None)
        page = s.search(f"account_id:{acct_a_id}", allowed_account_ids=None)
    finally:
        pool.close()
    ids = [r.message_id for r in page.results]
    assert ids == [mid_a2, mid_a1]


def test_sort_date_with_text_is_unbounded_lexical_paginated(db_dsn, db_conn):
    """``sort=date`` + non-empty free_text must behave like Gmail:
    SELECT all messages matching the term, ORDER BY date DESC, paginate
    by keyset — *not* bounded by ``rerank_pool_size``.

    Why: with the hybrid path, "e-ticket" returns at most
    ``rerank_pool_size`` (default 20) candidates fused by RRF, then
    sorted by date. A user with dozens of recent e-tickets only sees
    a handful (top-K by relevance), and "Load more" grow_pool re-runs
    just return the same top-K with overlap. The user wants
    "show me all my e-tickets, newest first" — that's a lexical
    keyset query, not a hybrid retrieval.
    """
    # Seed 30 matching messages — well above rerank_pool_size=20.
    now = datetime.now(timezone.utc)
    rows = [("a", f"e-ticket booking #{i:02d}", None,
             now - timedelta(hours=i)) for i in range(30)]
    ids = _seed_with_dates(db_conn, rows)
    # ids[0] is newest (i=0), ids[29] is oldest (i=29) — ORDER DESC by internal_date.
    cfg = SearchConfig()
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=None, rewriter=None)
        # Walk every page until the cursor goes None.
        all_ids: list[int] = []
        page = s.search("e-ticket", allowed_account_ids=None, page_size=10, sort="date")
        all_ids.extend(r.message_id for r in page.results)
        while page.next_keyset is not None:
            page = s.search("e-ticket", allowed_account_ids=None, page_size=10, sort="date",
                            keyset_cursor=page.next_keyset)
            all_ids.extend(r.message_id for r in page.results)
    finally:
        pool.close()
    # All 30 messages must appear, newest first.
    assert all_ids == ids


def test_sort_date_lexical_paginates_across_dated_then_null_tail(db_dsn, db_conn):
    """Lexical-date path must cleanly walk dated rows first, then the
    NULLS-LAST tail of un-backfilled rows, with no duplicates across the
    boundary.

    Why: the keyset WHERE clause includes
    ``OR COALESCE(internal_date, date_sent) IS NULL`` so the planner can
    transition from "still in dated portion" to "in NULLS tail" mid-walk
    without a separate query. The risk is double-emitting NULL rows on the
    transition page; ORDER BY ... DESC NULLS LAST + LIMIT keeps them out
    until dated is exhausted, but only an end-to-end walk proves it.
    """
    now = datetime.now(timezone.utc)
    rows: list[tuple[str, str, object, object]] = []
    # 5 dated rows (matching), newest → oldest.
    for i in range(5):
        rows.append(("a", f"invoice batch #{i:02d}", None, now - timedelta(hours=i)))
    # 3 NULL-date rows (matching), both date columns NULL.
    for i in range(3):
        rows.append(("a", f"invoice undated #{i:02d}", None, None))
    seeded = _seed_with_dates(db_conn, rows)
    dated_ids = seeded[:5]
    null_ids = seeded[5:]
    cfg = SearchConfig()
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=None, rewriter=None)
        all_ids: list[int] = []
        page = s.search("invoice", allowed_account_ids=None, page_size=2, sort="date")
        all_ids.extend(r.message_id for r in page.results)
        while page.next_keyset is not None:
            page = s.search("invoice", allowed_account_ids=None, page_size=2, sort="date",
                            keyset_cursor=page.next_keyset)
            all_ids.extend(r.message_id for r in page.results)
    finally:
        pool.close()
    # Dated rows newest-first, then NULL rows in id DESC (NULLS-LAST tail).
    expected = dated_ids + list(reversed(null_ids))
    assert all_ids == expected
    # No row appears twice — the dated→NULL boundary is the risky transition.
    assert len(all_ids) == len(set(all_ids))


def test_searcher_no_cache_returns_token_none(db_dsn, db_conn):
    _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=_Reranker(), rewriter=None)
        page = s.search("Berlin", allowed_account_ids=None, use_cache=False)
    finally:
        pool.close()
    assert page.search_token is None
