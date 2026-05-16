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
