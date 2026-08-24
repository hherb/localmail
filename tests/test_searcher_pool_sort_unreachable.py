# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The hybrid pool is only ever built for ``sort="rank"``.

``Searcher.search`` has three retrieval branches. The date-keyset branch
takes ``sort="date"`` with non-blank free text; the blank-query branch
takes everything with blank free text; so the hybrid pool branch — the
only one that caches a pool and the only reader of ``_build_results``'
``sort`` parameter — is reachable only as ``rank`` + non-blank text.

That makes ``_date_sort_key`` dead code. It is kept and documented rather
than deleted, so this test is what stops a later change adding
``sort_order`` handling "for symmetry" to a branch that never runs.
"""
from __future__ import annotations

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _seed(conn, n=6):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, f"Subject {i} test",
                 f"Body {i} content with the keyword test."),
            )
    conn.commit()


def test_a_cached_pool_always_records_sort_rank(db_dsn, db_conn):
    """Every pool the Searcher caches was built as a rank pool.

    Asserted through the public ``get_pool_metadata`` rather than the cache
    dict, so it survives a cache refactor.
    """
    _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    reached = 0
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=None)
        for sort in ("rank", "date", None):
            page = s.search("test", allowed_account_ids=None, page_size=2,
                            user_id=1, sort=sort)
            if page.search_token is None:
                continue
            reached += 1
            meta = s.get_pool_metadata(page.search_token, user_id=1)
            assert meta is not None
            assert meta.sort == "rank", (
                f"search(sort={sort!r}) cached a pool recording "
                f"sort={meta.sort!r}; _build_results' date branch is "
                "reachable after all and _date_sort_key is not dead code"
            )
            # The same argument one axis over: a pool is only minted on the
            # rank branch, and rank + asc is refused before any pool can be
            # built, so an ascending pool is unreachable too. Asserted
            # rather than assumed, because `reject_pool_sort_mismatch`'s
            # order half is dead only for as long as this holds.
            assert meta.sort_order == "desc", (
                f"search(sort={sort!r}) cached a pool recording "
                f"sort_order={meta.sort_order!r}; an ascending pool is "
                "reachable after all"
            )
    finally:
        pool.close()
    # Without this the loop could `continue` on every iteration and the test
    # would pass having asserted nothing — the vacuity guard
    # `test_mcp_tools.py` already carries for its own walk.
    assert reached, "no iteration cached a pool; the test proved nothing"
