# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Public Python API surface for localmail hybrid search.

Usage::

    from localmail.search import create_searcher, Searcher, SearchPage

    searcher = create_searcher()          # reads ~/.config/localmail/config.toml
    page = searcher.search("quarterly report", allowed_account_ids=None)

`allowed_account_ids` is required and has no default: `None` means "no ACL"
(the whole archive, full search-DSL power), while a list scopes every retrieval
arm to those accounts. Multi-user callers must pass the caller's grants.
"""

from __future__ import annotations

from localmail.search.query import ParsedQuery, QueryParseError, SearchFilters
from localmail.search.rewriter import QueryRewriter, RewriteParseError, RewriteResult
from localmail.search.rewriter_backends import (
    AnthropicRewriter,
    MissingApiKey,
    OllamaLLMRewriter,
    OpenAICompatRewriter,
    build_rewriter,
)
from localmail.search.searcher import SearchPage, SearchResult, Searcher

__all__ = [
    "create_searcher",
    "Searcher",
    "SearchPage",
    "SearchResult",
    "ParsedQuery",
    "SearchFilters",
    "QueryParseError",
    "QueryRewriter",
    "RewriteResult",
    "OllamaLLMRewriter",
    "OpenAICompatRewriter",
    "AnthropicRewriter",
    "build_rewriter",
    "MissingApiKey",
    "RewriteParseError",
]

_UNSET = object()  # sentinel: caller did not provide the argument


def create_searcher(
    cfg=None,
    *,
    dsn: str | None = None,
    embeddings=_UNSET,
    reranker=_UNSET,
    rewriter=_UNSET,
) -> Searcher:
    """Build and return a ready-to-use :class:`Searcher`.

    Parameters
    ----------
    cfg:
        A :class:`~localmail.config.LocalmailConfig` instance.  When *None*
        the default config file is loaded via :func:`~localmail.config.load_config`.
    dsn:
        Explicit Postgres DSN.  Takes precedence over ``cfg.database.dsn``.
    embeddings:
        An :class:`~localmail.search.embeddings.EmbeddingBackend` instance.
        When *None* a :class:`~localmail.search.embeddings.FastEmbedBackend`
        is constructed from ``cfg.search``.
    reranker:
        A :class:`~localmail.search.reranker.Reranker` instance.  When *None*
        and ``cfg.search.reranker_enabled`` is *True*, a
        :class:`~localmail.search.reranker.FastEmbedReranker` is built.
        Pass ``reranker=None`` together with a config where
        ``reranker_enabled=False`` to disable reranking entirely.
    """
    import logging

    from localmail.config import LocalmailConfig, load_config
    from localmail.db import open_pool
    from localmail.search.embeddings import FastEmbedBackend
    from localmail.search.reranker import FastEmbedReranker

    if cfg is None:
        cfg = load_config()

    effective_dsn = dsn or cfg.database.dsn
    pool = open_pool(effective_dsn)

    if embeddings is _UNSET:
        embeddings = FastEmbedBackend(cfg.search)

    if reranker is _UNSET:
        if cfg.search.reranker_enabled:
            try:
                reranker = FastEmbedReranker(cfg.search)
            except Exception as exc:
                logging.getLogger("localmail.search").warning(
                    "reranker init failed (%s=%r): %s — continuing without rerank",
                    "reranker_model", cfg.search.reranker_model, exc,
                )
                reranker = None
        else:
            reranker = None

    if rewriter is _UNSET:
        if cfg.search.rewriter_enabled_by_default:
            try:
                rewriter = build_rewriter(cfg.search)
            except Exception as exc:
                logging.getLogger("localmail.search").warning(
                    "rewriter init failed (backend=%r model=%r): %s — continuing without --smart",
                    cfg.search.rewriter_backend, cfg.search.rewriter_model, exc,
                )
                rewriter = None
            if rewriter is not None and cfg.search.rewriter_cache_size > 0:
                from localmail.search.rewrite_cache import CachingRewriter

                rewriter = CachingRewriter(
                    rewriter,
                    maxsize=cfg.search.rewriter_cache_size,
                    ttl_s=cfg.search.rewriter_cache_ttl_s,
                )
        else:
            rewriter = None

    return Searcher(
        pool=pool,
        cfg=cfg.search,
        embeddings=embeddings,
        reranker=reranker,
        rewriter=rewriter,
    )
