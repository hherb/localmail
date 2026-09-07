# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A NaN reranker cannot corrupt a real page (#361 review follow-up).

The behavioural half of the wiring pin; the structural half is
``test_rerank_wiring.py``. Both exist because either alone has a hole: the
AST rule reads the *spelling* of the call and cannot see a caller that
bypasses the guard some other way, and this test drives one call site and
cannot see the other.

Nothing pinned this when #361 shipped. Every test of ``_safe_rerank`` calls
it directly, so removing the call from ``Searcher.search`` — discarding
both the raise fallback and the value guard — left the suite green. The
handoff records that the authors verified the real path by hand, once,
against the live archive; that is the ``#278`` shape this tree has a name
for, and the remedy is to make the machine do it.

Seeded like ``test_searcher.py``, whose fixtures this mirrors.
"""
from __future__ import annotations

import math

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import Searcher


class _Embedder:
    name = "stub"
    model = "stub"
    dimension = 768

    def embed_documents(self, texts):
        return [[1.0 / (i + 1)] * 768 for i, _ in enumerate(texts)]

    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


class _NaNReranker:
    """A cross-encoder that fails on its first candidate and scores the rest
    on the negative-logit scale a real one uses (fastembed's own example is
    ``[-1.24, -10.6]``)."""

    name = "stub"
    model = "stub/nan-cross-encoder"

    def rerank(self, query, candidates):
        return [float("nan")] + [-1.0 * (i + 1) for i in range(len(candidates) - 1)]


def _seed(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = row[0]
        for i, (subject, body) in enumerate([
            ("Berlin conference next week", "Looking forward to Berlin"),
            ("Berlin lunch tomorrow", "Berlin, want to grab lunch?"),
            ("Berlin conference review", "How was the Berlin conference?"),
        ]):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, subject, body),
            )
    conn.commit()


def _search(db_dsn, db_conn, reranker):
    _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                            reranker=reranker, rewriter=None)
        return searcher.search("Berlin", allowed_account_ids=None)
    finally:
        pool.close()


def test_no_non_finite_score_reaches_a_real_page(db_dsn, db_conn) -> None:
    """The property, through the whole stack rather than through the helper.

    A surviving NaN here is silent on this path and a 500 on HTTP —
    Starlette renders with ``allow_nan=False``, so the response fails after
    the search has already succeeded, naming nothing.
    """
    page = _search(db_dsn, db_conn, _NaNReranker())
    assert page.results, "expected the seeded corpus to match"
    assert all(math.isfinite(r.score) for r in page.results), (
        [r.score for r in page.results]
    )


def test_the_page_is_ordered_and_the_unscored_row_is_not_first(
    db_dsn, db_conn,
) -> None:
    """Two properties one assertion apart, because the second is what the
    scale question is about: the reranker's own scores are negative, so a
    row substituted with its *fused RRF* score — positive, and bounded by
    ``1/61`` — would outrank every row the model actually judged.
    """
    page = _search(db_dsn, db_conn, _NaNReranker())
    scores = [r.score for r in page.results]
    assert scores == sorted(scores, reverse=True), scores
    if len(scores) > 1:
        assert scores[0] != min(scores), scores
        # The demoted row sorts last, so the page's worst score is the
        # substitute rather than the worst score the model produced.
        assert min(scores) < -1.0, scores
