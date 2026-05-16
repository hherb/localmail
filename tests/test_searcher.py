"""End-to-end Searcher tests against real Postgres with stubbed backends."""

from __future__ import annotations

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
