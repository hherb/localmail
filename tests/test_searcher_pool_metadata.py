# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for ``Searcher.get_pool_metadata`` and ``Searcher.config``.

These accessors replace the two ``# noqa: SLF001`` reads in
``localmail.api.search._continue_or_grow`` and ``run_search`` (issue #71).
They are the public boundary between the api/ layer's grow-pool transparent
recovery and the Searcher's internal page cache; a future change to the
cache's entry-dict shape must not silently break the route.
"""

from __future__ import annotations

import dataclasses
import time

import pytest

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import PoolMetadata, Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


class _R:
    name = "s"; model = "s"
    def rerank(self, q, c): return [1.0 - i * 0.001 for i, _ in enumerate(c)]


def _seed(conn, n: int = 20) -> None:
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


def test_config_property_returns_search_config(db_dsn):
    cfg = SearchConfig(page_size_default=5)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        assert s.config is cfg
        assert isinstance(s.config, SearchConfig)
        assert s.config.page_size_default == 5
    finally:
        pool.close()


def test_get_pool_metadata_returns_none_for_unknown_token(db_dsn):
    cfg = SearchConfig()
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        assert s.get_pool_metadata("nonexistent") is None
        assert s.get_pool_metadata("nonexistent", user_id=42) is None
    finally:
        pool.close()


def test_get_pool_metadata_returns_metadata_after_search(db_dsn, db_conn):
    _seed(db_conn, n=12)
    cfg = SearchConfig(page_size_default=3, candidates_per_arm=7,
                       rerank_pool_size=11)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        page = s.search("test")
        assert page.search_token is not None

        meta = s.get_pool_metadata(page.search_token)
    finally:
        pool.close()

    assert isinstance(meta, PoolMetadata)
    assert meta.candidates_per_arm == 7
    assert meta.page_size == 3
    assert meta.rerank_pool_size == 11
    # pool_size is the count of hydrated rows; bounded by rerank_pool_size
    # but determined by the actual retrieval result for the seed corpus.
    assert meta.pool_size > 0
    assert meta.pool_size <= 11


def test_get_pool_metadata_scoped_to_user_id(db_dsn, db_conn):
    _seed(db_conn, n=10)
    cfg = SearchConfig(page_size_default=5)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        page = s.search("test", user_id=1)
        token = page.search_token
        assert token is not None

        # Same user → hit.
        meta_alice = s.get_pool_metadata(token, user_id=1)
        # Different user → treated as miss (same invariant as continue_page).
        meta_bob = s.get_pool_metadata(token, user_id=2)
        # user_id=None bypasses the check (matches continue_page/grow_pool).
        meta_unspecified = s.get_pool_metadata(token, user_id=None)
    finally:
        pool.close()

    assert meta_alice is not None
    assert meta_bob is None
    assert meta_unspecified is not None


def test_get_pool_metadata_returns_none_after_ttl_expiry(db_dsn, db_conn):
    _seed(db_conn, n=5)
    # Ridiculously short TTL so the entry is stale by the time we look.
    cfg = SearchConfig(page_size_default=5, page_cache_ttl_s=0)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        page = s.search("test")
        token = page.search_token
        assert token is not None
        # PageCache compares against monotonic clock with > TTL; sleep
        # past the boundary so the next get evicts.
        time.sleep(0.01)
        meta = s.get_pool_metadata(token)
    finally:
        pool.close()

    assert meta is None


def test_get_pool_metadata_does_not_extend_ttl(db_dsn, db_conn):
    """Calling get_pool_metadata must not refresh the cache TTL.

    The accessor is for introspection; only a real consume (continue_page /
    grow_pool) should renew the entry's freshness. Otherwise a chatty
    grow-pool-cap probe would keep an exhausted pool alive indefinitely.

    Implementation note: PageCache.get currently does ``move_to_end`` on
    every read (LRU bump) but TTL is anchored to *insertion time*, not
    last access. So this test asserts the entry stops being readable after
    TTL elapses even with intervening reads — which is the contract callers
    actually rely on.
    """
    _seed(db_conn, n=5)
    cfg = SearchConfig(page_size_default=5, page_cache_ttl_s=0)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(), rewriter=None)
        page = s.search("test")
        token = page.search_token
        assert token is not None
        time.sleep(0.01)
        # Read once (would refresh if the accessor extended TTL).
        _ = s.get_pool_metadata(token)
        # And then once more.
        meta = s.get_pool_metadata(token)
    finally:
        pool.close()

    assert meta is None


def test_pool_metadata_is_frozen_dataclass():
    """PoolMetadata is a value object; immutability prevents downstream
    mutation from being mistaken for cache invalidation."""
    meta = PoolMetadata(candidates_per_arm=50, page_size=20,
                        rerank_pool_size=100, pool_size=80)
    with pytest.raises(dataclasses.FrozenInstanceError):
        meta.candidates_per_arm = 99  # type: ignore[misc]
