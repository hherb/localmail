"""Unit tests for EmbeddingBackend protocol + FastEmbedBackend wrapper.

The actual fastembed model load is slow + heavy (~250 MB); we test the
wrapper behaviour with a stub model and gate the real-model smoke test
under pytest.mark.slow.
"""

from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.search.embeddings import (
    EmbeddingBackend,
    EmbeddingConfigError,
    FastEmbedBackend,
    _resolve_model_path,
)


class _StubInner:
    """Stand-in for fastembed.TextEmbedding with deterministic output."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, texts, **_):
        for i, _t in enumerate(texts):
            yield [(i + 1) / 100.0] * self.dim

    def query_embed(self, texts, **_):
        for i, _t in enumerate(texts):
            yield [(i + 7) / 100.0] * self.dim


def test_fastembed_backend_protocol_attrs():
    be = FastEmbedBackend(cfg=SearchConfig(), inner=_StubInner(dim=768))
    assert be.name == "fastembed"
    assert be.dimension == 768
    assert be.model == "embeddinggemma"


def test_fastembed_backend_embed_documents_shape():
    be = FastEmbedBackend(cfg=SearchConfig(), inner=_StubInner(dim=768))
    vecs = be.embed_documents(["a", "b", "c"])
    assert len(vecs) == 3
    assert all(len(v) == 768 for v in vecs)


def test_fastembed_backend_embed_query_uses_query_path():
    be = FastEmbedBackend(cfg=SearchConfig(), inner=_StubInner(dim=768))
    v = be.embed_query("hello")
    # query_embed seeds with i+7 vs documents i+1 — proves correct path
    # i=0 for the first (only) text: (0+7)/100 = 0.07
    assert v[0] == pytest.approx(7 / 100.0)


def test_fastembed_backend_dim_mismatch_raises():
    cfg = SearchConfig(embedding_dim=1024)
    with pytest.raises(EmbeddingConfigError):
        FastEmbedBackend(cfg=cfg, inner=_StubInner(dim=768)).health_check()


def test_protocol_matched_by_backend():
    be: EmbeddingBackend = FastEmbedBackend(cfg=SearchConfig(), inner=_StubInner(dim=768))
    assert callable(be.embed_documents)
    assert callable(be.embed_query)
    assert callable(be.health_check)


def test_resolve_model_path_uses_registry_for_short_name():
    cfg = SearchConfig()  # embedding_model = "embeddinggemma"
    assert _resolve_model_path(cfg) == "google/embeddinggemma-300m"


def test_resolve_model_path_override_wins():
    cfg = SearchConfig(
        embedding_model="embeddinggemma",
        embedding_model_path="google/embeddinggemma-1b",
    )
    assert _resolve_model_path(cfg) == "google/embeddinggemma-1b"


def test_resolve_model_path_unknown_short_name_passes_through():
    """A short name not in the registry is assumed to already be a full path."""
    cfg = SearchConfig(embedding_model="some/other-model-v1")
    assert _resolve_model_path(cfg) == "some/other-model-v1"


def test_fastembed_backend_records_resolved_model_path():
    cfg = SearchConfig()
    be = FastEmbedBackend(cfg=cfg, inner=_StubInner(dim=768))
    assert be.model == "embeddinggemma"
    assert be.model_path == "google/embeddinggemma-300m"


class _NoQueryEmbedInner:
    """Older fastembed shape: no `query_embed`."""

    def embed(self, texts, **_):
        for _ in texts:
            yield [0.0] * 768


def test_fastembed_backend_raises_when_query_embed_missing():
    """If the installed fastembed lacks query_embed, query/document task
    prefixes would be wrong — fail loudly at init rather than degrade
    silently."""
    with pytest.raises(EmbeddingConfigError) as exc:
        FastEmbedBackend(cfg=SearchConfig(), inner=_NoQueryEmbedInner())
    assert "query_embed" in str(exc.value)


@pytest.mark.slow
def test_fastembed_backend_real_model_smoke(require_real_embedding_model):
    """Real model load. Opt-in: pytest -m slow. Skips if the model can't download."""
    cfg = SearchConfig()  # embeddinggemma default
    be = FastEmbedBackend(cfg=cfg)
    be.health_check()
    v = be.embed_query("the quick brown fox")
    assert len(v) == 768
    docs = be.embed_documents(["alpha", "beta"])
    assert len(docs) == 2 and len(docs[0]) == 768
