"""Unit tests for Reranker protocol + FastEmbedReranker wrapper."""

from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.search.reranker import FastEmbedReranker, Reranker


class _StubInner:
    """Stand-in for fastembed's cross-encoder; returns deterministic scores."""

    def rerank(self, query, documents, **_):
        return [
            {"index": i, "score": 1.0 / (i + 1)}
            for i in range(len(documents))
        ]


def test_fastembed_reranker_protocol_attrs():
    rr = FastEmbedReranker(cfg=SearchConfig(), inner=_StubInner())
    assert rr.name == "fastembed"
    assert rr.model == SearchConfig().reranker_model


def test_fastembed_reranker_returns_scores_in_input_order():
    rr = FastEmbedReranker(cfg=SearchConfig(), inner=_StubInner())
    scores = rr.rerank("q", ["a", "b", "c"])
    assert len(scores) == 3
    assert scores[0] == 1.0
    assert scores[1] == pytest.approx(0.5)
    assert scores[2] == pytest.approx(1 / 3)


def test_protocol_matched():
    rr: Reranker = FastEmbedReranker(cfg=SearchConfig(), inner=_StubInner())
    assert callable(rr.rerank)
