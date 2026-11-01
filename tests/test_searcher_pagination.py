# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for pagination via the page cache + grow_pool re-run."""

from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.page_cache import CacheMissError, PageOutOfPoolError
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0]*768 for _ in t]
    def embed_query(self, t): return [0.5]*768
    def health_check(self): pass


class _R:
    name = "s"; model = "s"
    def rerank(self, q, c): return [1.0 - i*0.001 for i, _ in enumerate(c)]


def _seed_many(conn, n=30):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i+1])*32, f"Subject {i} test",
                 f"Body {i} content with the keyword test."),
            )
    conn.commit()


def test_continue_page_returns_next_page_from_cache(db_dsn, db_conn):
    _seed_many(db_conn, n=30)
    cfg = SearchConfig(page_size_default=5)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        p1 = s.search("test", allowed_account_ids=None)
        p2 = s.continue_page(p1.search_token, page=2)
    finally:
        pool.close()
    assert p2.page == 2
    assert p2.results
    ids1 = {r.message_id for r in p1.results}
    ids2 = {r.message_id for r in p2.results}
    assert ids1.isdisjoint(ids2)


def test_continue_page_beyond_pool_raises(db_dsn, db_conn):
    _seed_many(db_conn, n=3)
    cfg = SearchConfig(page_size_default=5)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        p1 = s.search("test", allowed_account_ids=None)
        with pytest.raises(PageOutOfPoolError):
            s.continue_page(p1.search_token, page=2)
    finally:
        pool.close()


def test_grow_pool_returns_page_1_with_larger_pool(db_dsn, db_conn):
    _seed_many(db_conn, n=20)
    cfg = SearchConfig(page_size_default=5, candidates_per_arm=3, rerank_pool_size=3)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        p1 = s.search("test", allowed_account_ids=None)
        assert p1.pool_size <= 3
        p1_big = s.grow_pool(p1.search_token, candidates_per_arm=20, page=1)
    finally:
        pool.close()
    assert p1_big.candidates_per_arm == 20
    assert p1_big.pool_size > p1.pool_size
    assert p1_big.page == 1


def test_continue_page_with_invalid_token_raises(db_dsn, db_conn):
    cfg = SearchConfig()
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        with pytest.raises(CacheMissError):
            s.continue_page("nonexistent", page=2)
    finally:
        pool.close()


def _page_through(db_dsn, cfg, *, acl, replay_first_cursor=False):
    """Page ``test`` the way a client does: ``run_search`` with
    ``next_cursor`` until it is null. Returns ids served, per-page counts and
    tokens; optionally replays the first (page-2) cursor after the walk."""
    from localmail.api.search import run_search
    from localmail.api.search_cursor import decode_search_cursor

    pool = open_pool(db_dsn)
    seen: list[str] = []
    counts: list[int] = []
    cursors: list[str] = []
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        cursor = None
        for _ in range(50):
            out = run_search(searcher=s, free_text="test", filters={}, limit=5,
                             allowed_account_ids=acl, user_id=1, cursor=cursor)
            seen += [r["message_id"] for r in out["results"]]
            counts.append(len(out["results"]))
            cursor = out["next_cursor"]
            if cursor is None:
                break
            cursors.append(cursor)
        assert cursor is None
        if replay_first_cursor:
            run_search(searcher=s, free_text="test", filters={}, limit=5,
                       allowed_account_ids=acl, user_id=1, cursor=cursors[0])
    finally:
        pool.close()
    return seen, counts, {decode_search_cursor(c).token for c in cursors}


def _acl(db_conn) -> list[int]:
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM accounts")
        return [row[0] for row in cur.fetchall()]


def test_paging_past_a_grow_serves_each_message_once(db_dsn, db_conn):
    """#426: the page after a pool grow used to be the grown pool's page 1,
    so page 3 of a search repeated page 1. Every seeded message must arrive
    exactly once, under one token.

    Under the default ``candidates_per_arm_max`` (800): the grows that find
    nothing new double the arms to 400 and 800, where arm 4 used to ask
    pgvector for an ``ef_search`` past its 1000 ceiling — a 500 — and where
    an empty grow used to be served as an empty page with a cursor."""
    _seed_many(db_conn, n=30)
    cfg = SearchConfig(page_size_default=5, candidates_per_arm=4,
                       rerank_pool_size=4)
    assert cfg.candidates_per_arm_max * cfg.arm4_chunk_prefetch_multiplier > 1000
    run_embed_worker_once(db_conn, cfg, _E())
    seen, counts, tokens = _page_through(db_dsn, cfg, acl=_acl(db_conn))
    assert len(seen) == len(set(seen)), "a message was served twice"
    assert len(seen) == 30
    assert len(tokens) == 1
    assert 0 not in counts[:-1], f"an empty page carried a cursor: {counts}"


def test_a_cursor_from_before_the_grow_is_expired(db_dsn, db_conn):
    """A page served before a grow is not in the grown pool, so its cursor
    is a 409 — never a 200 slicing the grown pool from a negative start."""
    from localmail.api.errors import SearchCursorExpired

    _seed_many(db_conn, n=30)
    cfg = SearchConfig(page_size_default=5, candidates_per_arm=4,
                       rerank_pool_size=4, candidates_per_arm_max=64)
    run_embed_worker_once(db_conn, cfg, _E())
    with pytest.raises(SearchCursorExpired):
        _page_through(db_dsn, cfg, acl=_acl(db_conn), replay_first_cursor=True)


def test_a_grow_at_the_cap_serves_the_rest_of_the_pool(db_dsn, db_conn):
    """The first grow reaches the cap with more than two pages of unseen
    rows: its third page must still be reached. ``has_more_in_pool`` counts
    pages from the grow, not from page 1 — counted from page 1, the walk
    stops after the grown pool's second page (4 + 2 * 5 rows) (#426)."""
    _seed_many(db_conn, n=30)
    cfg = SearchConfig(page_size_default=5, candidates_per_arm=8,
                       rerank_pool_size=4, candidates_per_arm_max=16)
    run_embed_worker_once(db_conn, cfg, _E())
    seen, counts, _ = _page_through(db_dsn, cfg, acl=_acl(db_conn))
    assert len(seen) == len(set(seen))
    assert counts[0] == 4
    assert len(seen) > 4 + 2 * 5, counts
