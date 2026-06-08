"""Bounded, thread-safe LRU+TTL cache for ``--smart`` query rewrites.

A :class:`CachingRewriter` wraps any :class:`~localmail.search.rewriter.QueryRewriter`
and is transparent to the :class:`~localmail.search.searcher.Searcher`: it
implements the same Protocol (``name`` / ``model`` / ``rewrite``). The slow
inner ``rewrite`` (an Ollama HTTP call) runs outside the lock, so a cache miss
never blocks a concurrent hit. Only successful results are cached — failures
propagate uncached so a transient backend outage recovers on the next call.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import date
from typing import Callable

from localmail.search.rewriter import QueryRewriter, RewriteResult

_KEY_SEP = "\x00"  # NUL cannot appear in free_text (parser strips it)


class CachingRewriter:
    """Decorator that memoises rewrite results, keyed on (today, free_text).

    ``maxsize <= 0`` makes :meth:`rewrite` a pure pass-through (no dict, no
    lock acquisition) — the configured "off" switch.
    """

    def __init__(
        self,
        inner: QueryRewriter,
        *,
        maxsize: int,
        ttl_s: float,
        today_provider: Callable[[], date] = date.today,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._maxsize = maxsize
        self._ttl = ttl_s
        self._today = today_provider
        self._clock = clock
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, RewriteResult]] = OrderedDict()

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    def _key(self, free_text: str) -> str:
        return f"{self._today().isoformat()}{_KEY_SEP}{free_text}"

    def rewrite(self, free_text: str) -> RewriteResult:
        if self._maxsize <= 0:
            return self._inner.rewrite(free_text)

        key = self._key(free_text)
        hit = self._get(key)
        if hit is not None:
            return hit

        result = self._inner.rewrite(free_text)  # slow call, outside the lock
        self._put(key, result)
        return result

    def _get(self, key: str) -> RewriteResult | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            stamp, value = entry
            if self._clock() - stamp > self._ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def _put(self, key: str, value: RewriteResult) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (self._clock(), value)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)
