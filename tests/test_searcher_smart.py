"""Smart-path wiring tests: assert the rewrite call path + surfaced fall-through,
not retrieval quality."""

from __future__ import annotations

import logging

import httpx

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.query import SearchFilters
from localmail.search.rewriter import RewriteResult
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


class _R:
    name = "s"; model = "s"
    def rerank(self, q, c): return [1.0 - i * 0.001 for i, _ in enumerate(c)]


class FakeRewriter:
    name = "fake"; model = "fake"
    def __init__(self, result): self._result = result
    def rewrite(self, free_text): return self._result


class RaisingRewriter:
    name = "raise"; model = "raise"
    def rewrite(self, free_text): raise httpx.ConnectError("down")


class Status404Rewriter:
    name = "missing-model"; model = "granite4.1:3b-q8_0"
    def rewrite(self, free_text):
        request = httpx.Request("POST", "http://localhost:11434/api/generate")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)


def _seed_one(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, '<m1>', %s, 'Subject test', 'body test content',"
            " '{}'::jsonb, 'r', 1)",
            (acct, b"\x01" * 32),
        )
    conn.commit()


def _seed_subject(conn, subject):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, headers, raw_bytes, size_bytes)"
            " VALUES (%s, '<m1>', %s, %s, 'body', '{}'::jsonb, 'r', 1)",
            (acct, b"\x01" * 32, subject),
        )
    conn.commit()


def _smart_result():
    return RewriteResult(rewritten_text="rich", expansion_terms=["syn"],
                         extracted_filters=SearchFilters())


def test_smart_enriches_parsed_and_times_rewrite(db_dsn, db_conn):
    _seed_one(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(),
                     rewriter=FakeRewriter(_smart_result()))
        page = s.search("test", smart=True, use_cache=False)
    finally:
        pool.close()
    assert page.query.rewritten_text == "rich"
    assert page.query.expansion_terms == ["syn"]
    assert "rewrite" in page.timing_ms
    assert page.rewrite_status == "applied"
    assert page.rewrite_note is None
    assert page.rewrite_note_code is None


def test_smart_falls_through_on_rewriter_failure(db_dsn, db_conn, caplog):
    _seed_one(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(),
                     rewriter=RaisingRewriter())
        with caplog.at_level(logging.WARNING, logger="localmail.search"):
            page = s.search("test", smart=True, use_cache=False)
    finally:
        pool.close()
    assert page.rewrite_status == "failed"
    assert page.rewrite_note == "could not reach the rewriter service"
    assert page.rewrite_note_code == "unreachable"
    assert page.query.rewritten_text is None          # un-rewritten
    assert any("rewrite skipped" in r.message for r in caplog.records)


def test_smart_failed_404_yields_model_pull_note(db_dsn, db_conn):
    _seed_one(db_conn)
    cfg = SearchConfig(rewriter_model="granite4.1:3b-q8_0")
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(),
                     rewriter=Status404Rewriter())
        page = s.search("test", smart=True, use_cache=False)
    finally:
        pool.close()
    assert page.rewrite_status == "failed"
    assert "granite4.1:3b-q8_0" in page.rewrite_note
    assert "ollama pull granite4.1:3b-q8_0" in page.rewrite_note
    assert page.rewrite_note_code == "missing_model"


def test_smart_expansion_applies_on_sort_date_path(db_dsn, db_conn):
    """--smart --sort date must OR-in expansion terms too (the lexical-date
    branch shares build_lexical_tsquery with the hybrid arms)."""
    _seed_subject(db_conn, subject="receipt for lunch")
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _E())
    expand = RewriteResult(rewritten_text="invoice", expansion_terms=["receipt"],
                           extracted_filters=SearchFilters())
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=_R(),
                     rewriter=FakeRewriter(expand))
        # Without expansion, "invoice" matches nothing; with it, the
        # synonym-only "receipt" message must surface on the date path.
        plain = s.search("invoice", sort="date", use_cache=False)
        smart = s.search("invoice", smart=True, sort="date", use_cache=False)
    finally:
        pool.close()
    assert plain.results == []
    assert len(smart.results) == 1


def test_smart_available_true_when_rewriter_configured(db_dsn):
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=_R(),
                     rewriter=FakeRewriter(_smart_result()))
        assert s.smart_available is True
    finally:
        pool.close()


def test_smart_available_false_when_no_rewriter(db_dsn):
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=_R(),
                     rewriter=None)
        assert s.smart_available is False
    finally:
        pool.close()
