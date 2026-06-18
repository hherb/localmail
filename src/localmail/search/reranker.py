# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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
        # fastembed's TextCrossEncoder.rerank returns Iterable[float], one per
        # document in input order. (The older Iterable[{"index","score"}] dict
        # shape lives on rerank_pairs; don't conflate the two.)
        scores = [float(s) for s in self._inner.rerank(query, candidates)]
        if len(scores) != len(candidates):
            raise ValueError(
                f"reranker returned {len(scores)} scores for "
                f"{len(candidates)} candidates"
            )
        return scores
