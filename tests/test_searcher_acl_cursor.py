"""User-scoped cache invariants on Searcher.continue_page / grow_pool.

A search_token minted under user_id=A must NOT serve cached results to a
request from user_id=B — replays across users return a CacheMissError.
"""

from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.page_cache import CacheMissError
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


class _R:
    name = "s"; model = "s"
    def rerank(self, q, c): return [1.0 - i * 0.001 for i, _ in enumerate(c)]


def _seed(conn, n=10):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name,email_address,imap_host,auth_method)"
            " VALUES ('a','a@x','h','password') RETURNING id"
        )
        acct = cur.fetchone()[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (
                    acct, f"<m{i}>", bytes([i + 1]) * 32, f"Subject {i} test",
                    f"Body {i} content with the keyword test.",
                ),
            )
    conn.commit()


def test_continue_page_rejects_different_user(db_dsn, db_conn):
    _seed(db_conn, n=10)
    cfg = SearchConfig(page_size_default=5)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        p1 = s.search("test", user_id=1)
        # Alice's own user_id replays the cached pool.
        p1_again = s.continue_page(p1.search_token, page=1, user_id=1)
        assert p1_again.search_token == p1.search_token
        # Bob (user_id=2) sees a cache miss for Alice's token.
        with pytest.raises(CacheMissError):
            s.continue_page(p1.search_token, page=1, user_id=2)
    finally:
        pool.close()


def test_grow_pool_rejects_different_user(db_dsn, db_conn):
    _seed(db_conn, n=10)
    cfg = SearchConfig(page_size_default=5, candidates_per_arm=3, rerank_pool_size=3)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        p1 = s.search("test", user_id=1)
        with pytest.raises(CacheMissError):
            s.grow_pool(p1.search_token, candidates_per_arm=10, user_id=2)
    finally:
        pool.close()
