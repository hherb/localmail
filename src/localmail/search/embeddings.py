"""Embedding backend protocol + FastEmbedBackend (in-process ONNX).

Phase 1 ships fastembed only. OllamaBackend lands in Phase 4 alongside
the --smart query rewriter.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from localmail.config import SearchConfig


class EmbeddingConfigError(RuntimeError):
    """Raised at backend init / health_check when model/dim/config mismatch."""


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Embeds batches of texts into fixed-dim float vectors.

    Document and query paths are distinct because modern embedding models
    use task-specific instruction prefixes; the backend handles that
    internally so callers never pass the wrong one.
    """

    name: str
    model: str
    dimension: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
    def health_check(self) -> None: ...


def _build_fastembed_inner(cfg: SearchConfig) -> Any:
    """Build a real fastembed.TextEmbedding from config. Imports lazily."""
    from fastembed import TextEmbedding  # noqa: WPS433

    return TextEmbedding(
        model_name=f"google/{cfg.embedding_model}-300m"
        if cfg.embedding_model == "embeddinggemma"
        else cfg.embedding_model,
        cache_dir=str(cfg.fastembed_cache_dir) if cfg.fastembed_cache_dir else None,
        threads=cfg.fastembed_threads,
    )


class FastEmbedBackend:
    """In-process ONNX embedding via fastembed. Thread-safe after init."""

    name = "fastembed"

    def __init__(self, cfg: SearchConfig, inner: Any | None = None) -> None:
        self._cfg = cfg
        self.model = cfg.embedding_model
        self.dimension = cfg.embedding_dim
        self._inner = inner if inner is not None else _build_fastembed_inner(cfg)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [list(v) for v in self._inner.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        # Use query_embed if available (newer fastembed); fall back to embed.
        if hasattr(self._inner, "query_embed"):
            for v in self._inner.query_embed([text]):
                return list(v)
            raise EmbeddingConfigError("query_embed returned no vectors")
        for v in self._inner.embed([text]):
            return list(v)
        raise EmbeddingConfigError("embed returned no vectors")

    def health_check(self) -> None:
        """Verify that backend produces vectors of the configured dimension."""
        v = self.embed_query("health check probe")
        if len(v) != self.dimension:
            raise EmbeddingConfigError(
                f"backend produced dim={len(v)} but SearchConfig.embedding_dim={self.dimension}; "
                "either switch model or update embedding_dim"
            )
