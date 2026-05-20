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
        page = s.search("Berlin")
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
        page = s.search("Berlin")
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
        page = s.search("")
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
        page = s.search("berlin", sort="date")
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
        page = s.search("berlin")
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
        page = s.search(f"account_id:{acct_a_id}")
    finally:
        pool.close()
    ids = [r.message_id for r in page.results]
    assert ids == [mid_a2, mid_a1]


def test_searcher_no_cache_returns_token_none(db_dsn, db_conn):
    _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                     reranker=_Reranker(), rewriter=None)
        page = s.search("Berlin", use_cache=False)
    finally:
        pool.close()
    assert page.search_token is None
