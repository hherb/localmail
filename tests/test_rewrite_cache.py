from localmail.config import SearchConfig


def test_searchconfig_has_rewriter_cache_defaults():
    cfg = SearchConfig()
    assert cfg.rewriter_cache_size == 128
    assert cfg.rewriter_cache_ttl_s == 1200
import threading
from datetime import date as _date

import httpx
import pytest

from localmail.search.rewrite_cache import CachingRewriter
from localmail.search.query import SearchFilters
from localmail.search.rewriter import RewriteParseError, RewriteResult


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
