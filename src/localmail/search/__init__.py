"""Public Python API surface for localmail hybrid search.

Usage::

    from localmail.search import create_searcher, Searcher, SearchPage

    searcher = create_searcher()          # reads ~/.config/localmail/config.toml
    page = searcher.search("quarterly report")
"""

from __future__ import annotations

from localmail.search.query import ParsedQuery, QueryParseError, SearchFilters
from localmail.search.searcher import SearchPage, SearchResult, Searcher

__all__ = [
    "create_searcher",
    "Searcher",
    "SearchPage",
    "SearchResult",
    "ParsedQuery",
    "SearchFilters",
    "QueryParseError",
]

_UNSET = object()  # sentinel: caller did not provide the argument


def create_searcher(
    cfg=None,
    *,
    dsn: str | None = None,
    embeddings=_UNSET,
    reranker=_UNSET,
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
        reranker = FastEmbedReranker(cfg.search) if cfg.search.reranker_enabled else None

    return Searcher(
        pool=pool,
        cfg=cfg.search,
        embeddings=embeddings,
        reranker=reranker,
    )
