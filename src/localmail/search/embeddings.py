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


# Short-name → provider path registry. Operators can bypass this entirely
# by setting SearchConfig.embedding_model_path directly.
_MODEL_PATH_REGISTRY: dict[str, str] = {
    "embeddinggemma": "google/embeddinggemma-300m",
}


def _resolve_model_path(cfg: SearchConfig) -> str:
    """Resolve `cfg.embedding_model` (or override) to a full provider path.

    Order of precedence:
    1. `cfg.embedding_model_path` if set — used verbatim.
    2. `_MODEL_PATH_REGISTRY[cfg.embedding_model]` if known.
    3. `cfg.embedding_model` itself (assume it's already a full path).
    """
    if cfg.embedding_model_path:
        return cfg.embedding_model_path
    return _MODEL_PATH_REGISTRY.get(cfg.embedding_model, cfg.embedding_model)


def _build_fastembed_inner(cfg: SearchConfig) -> Any:
    """Build a real fastembed.TextEmbedding from config. Imports lazily."""
    from fastembed import TextEmbedding  # noqa: WPS433

    return TextEmbedding(
        model_name=_resolve_model_path(cfg),
        cache_dir=str(cfg.fastembed_cache_dir) if cfg.fastembed_cache_dir else None,
        threads=cfg.fastembed_threads,
    )


class FastEmbedBackend:
    """In-process ONNX embedding via fastembed. Thread-safe after init.

    Requires the installed fastembed version to expose `query_embed` — for
    task-prefixed models (EmbeddingGemma and friends) document and query
    embeddings are NOT interchangeable, and silently falling back to `embed`
    for query paths would produce document-shaped vectors and quietly
    degrade retrieval quality.
    """

    name = "fastembed"

    def __init__(self, cfg: SearchConfig, inner: Any | None = None) -> None:
        self._cfg = cfg
        self.model = cfg.embedding_model
        self.model_path = _resolve_model_path(cfg)
        self.dimension = cfg.embedding_dim
        self._inner = inner if inner is not None else _build_fastembed_inner(cfg)
        if not hasattr(self._inner, "query_embed"):
            raise EmbeddingConfigError(
                f"fastembed backend {type(self._inner).__name__!r} does not expose "
                "`query_embed`; query/document task prefixes would be wrong. "
                "Upgrade fastembed (>=0.7) or wire a backend that supports query embedding."
            )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [list(v) for v in self._inner.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        for v in self._inner.query_embed([text]):
            return list(v)
        raise EmbeddingConfigError("query_embed returned no vectors")

    def health_check(self) -> None:
        """Verify that backend produces vectors of the configured dimension."""
        v = self.embed_query("health check probe")
        if len(v) != self.dimension:
            raise EmbeddingConfigError(
                f"backend produced dim={len(v)} but SearchConfig.embedding_dim={self.dimension}; "
                "either switch model or update embedding_dim"
            )
