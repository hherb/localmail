"""Sanity tests for the public localmail.search API surface."""

from __future__ import annotations

import pytest


def test_public_names_importable():
    """All seven public names are importable from localmail.search."""
    from localmail.search import (  # noqa: F401
        QueryParseError,
        ParsedQuery,
        SearchFilters,
        SearchPage,
        SearchResult,
        Searcher,
        create_searcher,
    )


def test_create_searcher_degrades_when_reranker_init_fails(db_dsn, caplog, monkeypatch):
    """A reranker init failure (e.g. fastembed dropped the configured model name)
    must not disable search — Searcher should be returned with _reranker=None
    and a WARNING logged."""
    import logging

    from localmail.config import LocalmailConfig
    from localmail.search import Searcher, create_searcher

    class _StubEmbedder:
        name = "stub"
        model = "stub"
        dimension = 768

        def embed_documents(self, texts):
            return [[0.5] * 768 for _ in texts]

        def embed_query(self, text):
            return [0.5] * 768

        def health_check(self):
            pass

    def _boom(cfg):
        raise RuntimeError("Model X is not supported in TextCrossEncoder.")

    monkeypatch.setattr("localmail.search.reranker.FastEmbedReranker", _boom)

    # Default is `reranker_enabled=False` (so CPU-bound rerank doesn't blow
    # past request timeouts via the pagination grow_pool path). This test
    # exercises the opt-in "operator enabled rerank but model load failed"
    # graceful-degrade path, so flip the flag back on.
    cfg = LocalmailConfig.model_validate({
        "database": {"dsn": db_dsn}, "accounts": [],
        "search": {"reranker_enabled": True},
    })
    with caplog.at_level(logging.WARNING, logger="localmail.search"):
        searcher = create_searcher(cfg=cfg, embeddings=_StubEmbedder())
    try:
        assert isinstance(searcher, Searcher)
        assert searcher._reranker is None
        assert any(
            "reranker init failed" in rec.message for rec in caplog.records
        ), caplog.records
    finally:
        searcher._pool.close()


def test_create_searcher_returns_searcher(db_dsn):
    """create_searcher builds a Searcher with a live pool; pool closes cleanly."""
    from localmail.config import LocalmailConfig
    from localmail.search import Searcher, create_searcher
    from localmail.search.embeddings import FastEmbedBackend
    from localmail.config import SearchConfig

    class _StubEmbedder:
        name = "stub"
        model = "stub"
        dimension = 768

        def embed_documents(self, texts):
            return [[0.5] * 768 for _ in texts]

        def embed_query(self, text):
            return [0.5] * 768

        def health_check(self):
            pass

    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}, "accounts": []})
    searcher = create_searcher(cfg=cfg, embeddings=_StubEmbedder(), reranker=None)
    assert isinstance(searcher, Searcher)
    searcher._pool.close()


def test_searcher_returns_attachment_snippet(db_conn) -> None:
    """When a query is best answered by attachment content, Searcher returns
    a SearchResult with snippet_source='attachment' and attachment_filename
    populated from the carrying message's JSONB attachments."""
    import hashlib
    import json
    from localmail.config import SearchConfig
    from localmail.db import open_pool
    from localmail.search.embeddings import FastEmbedBackend
    from localmail.search.searcher import Searcher
    from tests.conftest import TEST_DSN

    sha = hashlib.sha256(b"contract details").digest()
    sha_hex = sha.hex()
    attachments = json.dumps(
        [{"filename": "contract.pdf", "sha256": sha_hex}]
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('c','e@z','h','password') RETURNING id"
        )
        row = cur.fetchone(); assert row is not None
        acct_id = row[0]
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s)",
            (sha, "/p", "application/pdf", 100),
        )
        cur.execute(
            "INSERT INTO messages "
            "(account_id, message_id, raw_sha256, subject, body_text, "
            " headers, raw_bytes, size_bytes, attachments) "
            "VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s, %s::jsonb) "
            "RETURNING id",
            (acct_id, "<contract@z>", b"\x20" * 32, "FYI", "see attached",
             b"r", 1, attachments),
        )

    cfg = SearchConfig()
    backend = FastEmbedBackend(cfg)

    qvec = backend.embed_query(
        "non-disclosure obligations under section 5"
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_chunks (sha256, chunk_idx, text, "
            "token_count, embedding_v1, embedded_at) "
            "VALUES (%s, 0, %s, 10, %s::halfvec, now())",
            (sha, "non-disclosure obligations under section 5", qvec),
        )
    db_conn.commit()

    pool = open_pool(TEST_DSN)
    try:
        searcher = Searcher(
            pool=pool, cfg=cfg, embeddings=backend,
            reranker=None, rewriter=None,
        )
        page = searcher.search(
            "non-disclosure obligations", page_size=10
        )
    finally:
        pool.close()

    att_results = [
        r for r in page.results if r.snippet_source == "attachment"
    ]
    assert att_results, (
        f"expected at least one attachment snippet; "
        f"got {[(r.subject, r.snippet_source) for r in page.results]}"
    )
    assert att_results[0].attachment_filename == "contract.pdf"
