"""Unit tests for Reranker protocol + FastEmbedReranker wrapper."""

from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.search.reranker import FastEmbedReranker, Reranker


class _StubInner:
    """Stand-in for fastembed.TextCrossEncoder.

    Mirrors the real signature: ``rerank(query, documents)`` yields one
    ``float`` per document, in input order.
    """

    def rerank(self, query, documents, **_):
        docs = list(documents)
        return [1.0 / (i + 1) for i in range(len(docs))]


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


def test_fastembed_reranker_rejects_length_mismatch():
    class _BrokenInner:
        def rerank(self, query, documents, **_):
            return [0.5]  # always one score, regardless of input length

    rr = FastEmbedReranker(cfg=SearchConfig(), inner=_BrokenInner())
    with pytest.raises(ValueError, match="2 candidates"):
        rr.rerank("q", ["a", "b"])


def test_protocol_matched():
    rr: Reranker = FastEmbedReranker(cfg=SearchConfig(), inner=_StubInner())
    assert callable(rr.rerank)


def test_safe_rerank_falls_back_when_reranker_raises(caplog):
    """If the reranker raises, search must degrade to RRF scores, not 500."""
    import logging

    from localmail.search.searcher import _safe_rerank

    class _BlowingReranker:
        name = "fastembed"
        model = "bogus/model"

        def rerank(self, query, candidates):
            raise TypeError("'float' object is not subscriptable")

    fallback = [0.9, 0.5, 0.1]
    with caplog.at_level(logging.WARNING, logger="localmail.search.searcher"):
        scores = _safe_rerank(
            _BlowingReranker(), "q", ["a", "b", "c"], fallback=fallback,
        )
    assert scores == fallback
    assert any(
        "falling back to fused RRF scores" in rec.message for rec in caplog.records
    ), caplog.records


def test_safe_rerank_passes_through_on_success():
    from localmail.search.searcher import _safe_rerank

    class _OkReranker:
        name = "fastembed"
        model = "stub"

        def rerank(self, query, candidates):
            return [0.1, 0.2, 0.3]

    scores = _safe_rerank(_OkReranker(), "q", ["a", "b", "c"], fallback=[0.0, 0.0, 0.0])
    assert scores == [0.1, 0.2, 0.3]
