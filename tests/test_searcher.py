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
    """Seed messages with explicit (account_name, subject, date_received).

    Returns the list of inserted message IDs in seed order.
    """
    ids = []
    accounts: dict[str, int] = {}
    with conn.cursor() as cur:
        for i, (acct_name, subject, date_received) in enumerate(rows):
            if acct_name not in accounts:
                cur.execute(
                    "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                    " VALUES (%s, %s, 'h', 'password') RETURNING id",
                    (acct_name, f"{acct_name}@x"),
                )
                accounts[acct_name] = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, date_received)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1, %s) RETURNING id",
                (accounts[acct_name], f"<m{i}>", bytes([i + 1]) * 32, subject, "body", date_received),
            )
            ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


def test_searcher_empty_query_returns_messages_by_date_received_desc(db_dsn, db_conn):
    """An empty free-text query is the canonical "show me my mail" default.

    The hybrid pipeline degenerates badly for empty queries: BM25 arms
    return [] (no terms to match), and vector arms rank by cosine distance
    to the embedding of the empty string — producing exactly
    `rerank_pool_size` (default 20) arbitrary-looking hits. The Searcher
    must detect this and fall back to `SELECT messages ORDER BY
    date_received DESC` so callers (GUI, MCP, programmatic) see a
    predictable list of recent mail.
    """
    now = datetime.now(timezone.utc)
    mid_oldest, mid_middle, mid_newest = _seed_with_dates(db_conn, [
        ("a", "old archive mail", now - timedelta(days=30)),
        ("a", "from last week", now - timedelta(days=7)),
        ("a", "fresh arrival", now - timedelta(hours=1)),
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
    assert ids == [mid_newest, mid_middle, mid_oldest]


def test_searcher_empty_query_respects_account_filter(db_dsn, db_conn):
    """The empty-query fallback must still honor structured filters — an
    account scope clicked in the GUI tree should narrow the recent-mail
    list, not be discarded along with the (absent) free-text query.
    """
    now = datetime.now(timezone.utc)
    mid_a1, mid_b1, mid_a2 = _seed_with_dates(db_conn, [
        ("acct_a", "a-1", now - timedelta(days=5)),
        ("acct_b", "b-1", now - timedelta(days=3)),
        ("acct_a", "a-2", now - timedelta(days=1)),
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
