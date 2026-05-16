"""Cross-encoder reranker protocol + FastEmbed implementation.

Used by Searcher after RRF fusion to re-score the candidate pool with a
model that sees (query, candidate) together — much higher quality than
the dual-encoder embeddings on their own.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from localmail.config import SearchConfig


@runtime_checkable
class Reranker(Protocol):
    name: str
    model: str

    def rerank(self, query: str, candidates: list[str]) -> list[float]: ...


def _build_fastembed_inner(cfg: SearchConfig) -> Any:
    """Lazily import + construct the underlying fastembed reranker."""
    from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: WPS433

    return TextCrossEncoder(model_name=cfg.reranker_model)


class FastEmbedReranker:
    """ONNX cross-encoder via fastembed. Returns one float per candidate."""

    name = "fastembed"

    def __init__(self, cfg: SearchConfig, inner: Any | None = None) -> None:
        self._cfg = cfg
        self.model = cfg.reranker_model
        self._inner = inner if inner is not None else _build_fastembed_inner(cfg)

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        if not candidates:
            return []
        # fastembed's API: rerank returns scored results; preserve input order
        raw = list(self._inner.rerank(query, candidates))
        scores = [0.0] * len(candidates)
        for entry in raw:
            scores[entry["index"]] = float(entry["score"])
        return scores
