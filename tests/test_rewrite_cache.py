# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import threading
from datetime import date as _date
from unittest import mock

import httpx
import pytest

from localmail.config import LocalmailConfig, SearchConfig
from localmail.search import create_searcher
from localmail.search.query import SearchFilters
from localmail.search.rewrite_cache import CachingRewriter
from localmail.search.rewriter import (
    OllamaLLMRewriter,
    RewriteParseError,
    RewriteResult,
)


def test_searchconfig_has_rewriter_cache_defaults():
    cfg = SearchConfig()
    assert cfg.rewriter_cache_size == 128
    assert cfg.rewriter_cache_ttl_s == 1200


class FakeRewriter:
    """A QueryRewriter that counts calls and returns a deterministic result."""

    name = "fake"
    model = "fake-model"

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._raises = raises
        self.closed = False

    def rewrite(self, free_text: str) -> RewriteResult:
        self.calls.append(free_text)
        if self._raises is not None:
            raise self._raises
        return RewriteResult(
            rewritten_text=f"rewritten:{free_text}",
            expansion_terms=[free_text],
            extracted_filters=SearchFilters(),
        )

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _const_today(d=_date(2026, 6, 9)):
    return lambda: d


def test_hit_skips_inner_call():
    inner = FakeRewriter()
    cache = CachingRewriter(
        inner, maxsize=128, ttl_s=1200, today_provider=_const_today(), clock=FakeClock()
    )
    first = cache.rewrite("tax return")
    second = cache.rewrite("tax return")
    assert first == second
    assert inner.calls == ["tax return"]


def test_distinct_queries_each_call_inner():
    inner = FakeRewriter()
    cache = CachingRewriter(
        inner, maxsize=128, ttl_s=1200, today_provider=_const_today(), clock=FakeClock()
    )
    cache.rewrite("a")
    cache.rewrite("b")
    assert inner.calls == ["a", "b"]


def test_ttl_expiry_recalls_inner():
    inner = FakeRewriter()
    clock = FakeClock()
    cache = CachingRewriter(
        inner, maxsize=128, ttl_s=100.0, today_provider=_const_today(), clock=clock
    )
    cache.rewrite("q")
    clock.t = 100.5
    cache.rewrite("q")
    assert inner.calls == ["q", "q"]


def test_date_is_part_of_key():
    inner = FakeRewriter()
    today = {"d": _date(2026, 6, 9)}
    cache = CachingRewriter(
        inner,
        maxsize=128,
        ttl_s=1200,
        today_provider=lambda: today["d"],
        clock=FakeClock(),
    )
    cache.rewrite("q")
    today["d"] = _date(2026, 6, 10)
    cache.rewrite("q")
    assert inner.calls == ["q", "q"]


def test_lru_evicts_least_recently_used():
    inner = FakeRewriter()
    cache = CachingRewriter(
        inner, maxsize=2, ttl_s=1200, today_provider=_const_today(), clock=FakeClock()
    )
    cache.rewrite("a")
    cache.rewrite("b")
    cache.rewrite("a")
    cache.rewrite("c")
    cache.rewrite("a")
    cache.rewrite("b")
    assert inner.calls == ["a", "b", "c", "b"]


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("down"), RewriteParseError("bad json")],
)
def test_failures_are_not_cached(exc):
    inner = FakeRewriter(raises=exc)
    cache = CachingRewriter(
        inner, maxsize=128, ttl_s=1200, today_provider=_const_today(), clock=FakeClock()
    )
    with pytest.raises(type(exc)):
        cache.rewrite("q")
    with pytest.raises(type(exc)):
        cache.rewrite("q")
    assert inner.calls == ["q", "q"]


def test_maxsize_zero_is_pass_through():
    inner = FakeRewriter()
    cache = CachingRewriter(
        inner, maxsize=0, ttl_s=1200, today_provider=_const_today(), clock=FakeClock()
    )
    cache.rewrite("q")
    cache.rewrite("q")
    assert inner.calls == ["q", "q"]
    assert len(cache._data) == 0


def test_name_model_and_close_delegate():
    inner = FakeRewriter()
    cache = CachingRewriter(
        inner, maxsize=128, ttl_s=1200, today_provider=_const_today(), clock=FakeClock()
    )
    assert cache.name == "fake"
    assert cache.model == "fake-model"
    cache.close()
    assert inner.closed is True


def test_concurrent_hot_key_does_not_corrupt():
    inner = FakeRewriter()
    cache = CachingRewriter(
        inner, maxsize=128, ttl_s=1200, today_provider=_const_today(), clock=FakeClock()
    )
    results: list[RewriteResult] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            r = cache.rewrite("hot")
            with lock:
                results.append(r)
        except BaseException as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 16
    assert all(r == results[0] for r in results)
    assert len(cache._data) == 1


def _cfg(*, cache_size: int) -> LocalmailConfig:
    # LocalmailConfig (alias of Config) requires a database dsn; supply a dummy
    # since open_pool is patched out and never actually connects.
    cfg = LocalmailConfig(database={"dsn": "postgresql:///localmail_test"})
    cfg.search.rewriter_enabled_by_default = True
    cfg.search.rewriter_cache_size = cache_size
    return cfg


def test_create_searcher_wraps_rewriter_when_cache_enabled():
    cfg = _cfg(cache_size=128)
    with mock.patch("localmail.db.open_pool"), \
         mock.patch("localmail.search.embeddings.FastEmbedBackend"):
        searcher = create_searcher(cfg)
    assert isinstance(searcher._rewriter, CachingRewriter)
    assert isinstance(searcher._rewriter._inner, OllamaLLMRewriter)


def test_create_searcher_leaves_rewriter_bare_when_cache_disabled():
    cfg = _cfg(cache_size=0)
    with mock.patch("localmail.db.open_pool"), \
         mock.patch("localmail.search.embeddings.FastEmbedBackend"):
        searcher = create_searcher(cfg)
    assert isinstance(searcher._rewriter, OllamaLLMRewriter)


def test_create_searcher_degrades_when_cloud_key_missing(monkeypatch):
    # A cloud backend with no API key raises MissingApiKey at construction;
    # create_searcher's guard must swallow it and run with no rewriter.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LocalmailConfig(database={"dsn": "postgresql:///localmail_test"})
    cfg.search.rewriter_enabled_by_default = True
    cfg.search.rewriter_backend = "openai"
    with mock.patch("localmail.db.open_pool"), \
         mock.patch("localmail.search.embeddings.FastEmbedBackend"):
        searcher = create_searcher(cfg)
    assert searcher._rewriter is None
    assert searcher.smart_available is False
