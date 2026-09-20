# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A caller-sized snippet window, honoured per page (kastellan slice E).

Snippets are built per page from the full cached source text, so a width can
widen as well as shrink and a continuation page takes its own.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import psycopg
import pytest

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import Searcher

# Filler words carry no query term, so the window is centred on the one
# "zebra" and every width up to the chunk length is fully used.
_FILLER = " ".join(f"word{i}" for i in range(400))  # ~3 KB


class _E:
    """Chunks containing "zebra" point at the query; every other chunk points
    away. A constant embedder ties every chunk in the vector arm, and the
    tie-break (#360) could then pick a header or a zebra-less body chunk as
    the snippet source."""
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t):
        return [[1.0 if "zebra" in x else -1.0] * 768 for x in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _seed(conn: psycopg.Connection, n: int = 2) -> None:
    """`n` messages whose ONLY "zebra"-like word is mid-body, so the body
    chunk (long) is the snippet source rather than the header chunk (short).

    The body carries "zebras", not "zebra" — deliberately not a match for
    ``plainto_tsquery('simple', 'zebra')`` (the 'simple' dictionary does not
    stem). That keeps this message off arm_bm25_messages and arm_bm25_chunks
    entirely, so ``rrf_fuse``'s per-message winner-chunk pick (searcher.py,
    "the winner-chunk `max()` ... is still decided by arm order on an exact
    tie", #360) never sees a competing ``(None, "message")`` key: whole-message
    BM25 (arm 1) and the whole-message string it falls back to (a bare
    ``message_id``, chunk_id=None) always rank #1 for a lone matching
    message, tying byte-for-byte with the chunk-level arms' own #1 (both are
    `1/(k+1)`) and losing that tie to arm 1 because it runs first — which is
    exactly the header-over-body flake this fixture exists to avoid. Only the
    vector arm (driven by the mock embedder's substring check, which still
    finds "zebra" inside "zebras") ever matches, so the sole
    ``(chunk_id, "message_chunks")`` key wins by construction. `make_snippet`
    and the `"zebra" in snip` assertions still work: both use substring
    search, not tokenised matching.
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = row[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, f"Note {i}",
                 f"{_FILLER} zebras {_FILLER}"),
            )
    conn.commit()


@pytest.fixture
def searcher(db_dsn, db_conn):
    _seed(db_conn)
    cfg = SearchConfig(page_size_default=1)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        yield Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=None,
                       rewriter=None)
    finally:
        pool.close()


def _snippet(page) -> str:
    [hit] = page.results
    assert hit.snippet_source == "body", "fixture must select the long body chunk"
    return hit.snippet


def test_omitted_is_byte_identical_to_the_configured_width(searcher) -> None:
    default = _snippet(searcher.search("zebra", allowed_account_ids=None))
    explicit = _snippet(searcher.search("zebra", allowed_account_ids=None,
                                        snippet_chars=200))
    assert default == explicit
    assert 150 < len(default) <= 202


def test_a_wider_window_is_honoured(searcher) -> None:
    snip = _snippet(searcher.search("zebra", allowed_account_ids=None,
                                    snippet_chars=500))
    assert len(snip) > 400
    assert "zebra" in snip


def test_a_narrower_window_is_honoured(searcher) -> None:
    snip = _snippet(searcher.search("zebra", allowed_account_ids=None,
                                    snippet_chars=50))
    assert len(snip) <= 52  # window + at most two ellipsis marks
    assert "zebra" in snip


def test_a_continuation_page_takes_its_own_width(searcher) -> None:
    p1 = searcher.search("zebra", allowed_account_ids=None, snippet_chars=500)
    p2 = searcher.continue_page(p1.search_token, page=2, snippet_chars=60)
    assert len(_snippet(p1)) > 400
    assert len(_snippet(p2)) <= 62


def test_continue_page_without_a_width_uses_the_default(searcher) -> None:
    p1 = searcher.search("zebra", allowed_account_ids=None, snippet_chars=500)
    p2 = searcher.continue_page(p1.search_token, page=2)
    assert 150 < len(_snippet(p2)) <= 202


def test_grow_pool_takes_its_own_width(searcher) -> None:
    p1 = searcher.search("zebra", allowed_account_ids=None)
    grown = searcher.grow_pool(p1.search_token, candidates_per_arm=100,
                               snippet_chars=40)
    assert len(_snippet(grown)) <= 42


@pytest.mark.parametrize("bad", [0, -5, True, False, "10", 1.5])
def test_a_non_positive_width_is_refused_before_any_io(bad) -> None:
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("touched the pool")
    s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=None, reranker=None,
                 rewriter=None)
    with pytest.raises(ValueError, match="snippet_chars"):
        s.search("zebra", allowed_account_ids=None, snippet_chars=bad)
    pool.connection.assert_not_called()


def test_sort_membership_is_checked_before_snippet_width() -> None:
    """Both guards sit before any IO (#348/#349), and membership comes first
    (#348) — the two-layers-order-one-rule-differently shape this codebase
    keeps re-learning. A malformed ``sort`` and a malformed ``snippet_chars``
    on the same call must surface the membership diagnosis, never the
    snippet one, so a caller who mistyped both sees the guard that runs
    first at the api boundary too (`run_search` checks membership ahead of
    `snippet_width_error`)."""
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("touched the pool")
    s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=None, reranker=None,
                 rewriter=None)
    with pytest.raises(ValueError, match="unknown sort"):
        s.search("zebra", allowed_account_ids=None, sort="Date",
                 snippet_chars=0)
    pool.connection.assert_not_called()


@pytest.mark.parametrize("method", ["continue_page", "grow_pool"])
def test_continuations_refuse_a_non_positive_width_too(method) -> None:
    s = Searcher(pool=MagicMock(), cfg=SearchConfig(), embeddings=None,
                 reranker=None, rewriter=None)
    with pytest.raises(ValueError, match="snippet_chars"):
        getattr(s, method)("tok", 2, snippet_chars=0)


@pytest.mark.parametrize("method", ["_build_results", "_search_with_parsed"])
def test_snippet_width_is_keyword_only_with_no_default(method: str) -> None:
    """The #234 shape, asserted rather than only claimed in a docstring.

    Both methods' docstrings say a default would let a call site forget the
    parameter and silently serve the configured width to a caller who asked
    for another. Every call site passes it today, so giving either one a
    default leaves the whole suite green and the guarantee gone — the
    `allowed_account_ids` pin in test_search_acl_clamp.py exists for exactly
    this reason.
    """
    import inspect

    param = inspect.signature(getattr(Searcher, method)).parameters["snippet_width"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
