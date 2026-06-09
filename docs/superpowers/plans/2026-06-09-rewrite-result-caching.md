# Rewrite-result Caching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded, thread-safe, per-process LRU+TTL cache for `--smart` query rewrites so repeated identical smart queries skip a fresh Ollama call.

**Architecture:** A `CachingRewriter` decorator wraps any `QueryRewriter` (same Protocol: `name`, `model`, `rewrite`). It is wired in `create_searcher` around the `OllamaLLMRewriter`; the `Searcher` is untouched. Cache key is `(today, free_text)`; only successful results are cached; failures propagate uncached. Internals are an `OrderedDict` + `threading.Lock` (the rewriter is shared across concurrent MCP requests).

**Tech Stack:** Python 3.12, `dataclasses`, `threading`, `collections.OrderedDict`, pytest. No new dependency, no migration.

Spec: [docs/superpowers/specs/2026-06-09-rewrite-result-caching-design.md](2026-06-09-rewrite-result-caching-design.md).

---

## File Structure

- **Create** `src/localmail/search/rewrite_cache.py` — the `CachingRewriter` class (one responsibility: cache rewrite results).
- **Modify** `src/localmail/config.py` — two new `SearchConfig` fields.
- **Modify** `src/localmail/search/__init__.py` — wrap the rewriter in `create_searcher`.
- **Modify** `config.example.toml` — document the two knobs.
- **Modify** `README.md` — document the two knobs (config table / search section).
- **Create** `tests/test_rewrite_cache.py` — unit tests for `CachingRewriter` + a `create_searcher` wiring test.

---

## Task 1: Config knobs

**Files:**
- Modify: `src/localmail/config.py` (the `SearchConfig` class, `# --- query rewriter (Phase 4) ---` block around line 350-355)
- Test: `tests/test_rewrite_cache.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_rewrite_cache.py` with:

```python
from localmail.config import SearchConfig


def test_searchconfig_has_rewriter_cache_defaults():
    cfg = SearchConfig()
    assert cfg.rewriter_cache_size == 128
    assert cfg.rewriter_cache_ttl_s == 1200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_cache.py::test_searchconfig_has_rewriter_cache_defaults -v`
Expected: FAIL — `AttributeError: 'SearchConfig' object has no attribute 'rewriter_cache_size'`.

- [ ] **Step 3: Add the two fields**

In `src/localmail/config.py`, immediately after `rewriter_max_expansion_terms: int = 8` (the last line of the rewriter block):

```python
    # Bounded per-process LRU+TTL cache for `--smart` rewrite results, keyed on
    # (today, free_text). Repeated identical smart queries skip a fresh Ollama
    # call. Entries are tiny, so the size can exceed page_cache_size. 0 disables.
    rewriter_cache_size: int = 128
    rewriter_cache_ttl_s: int = 1200
```

- [ ] **Step 4: Run test to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_cache.py::test_searchconfig_has_rewriter_cache_defaults -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/config.py tests/test_rewrite_cache.py
git commit -m "feat(search): add rewriter_cache_size/_ttl_s config knobs"
```

---

## Task 2: `CachingRewriter` — hit/miss core

**Files:**
- Create: `src/localmail/search/rewrite_cache.py`
- Test: `tests/test_rewrite_cache.py`

- [ ] **Step 1: Write the failing tests + the shared `FakeRewriter` fixture**

Append to `tests/test_rewrite_cache.py`:

```python
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
    assert inner.calls == ["tax return"]  # inner called exactly once


def test_distinct_queries_each_call_inner():
    inner = FakeRewriter()
    cache = CachingRewriter(
        inner, maxsize=128, ttl_s=1200, today_provider=_const_today(), clock=FakeClock()
    )
    cache.rewrite("a")
    cache.rewrite("b")
    assert inner.calls == ["a", "b"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_cache.py -k "hit_skips or distinct_queries" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'localmail.search.rewrite_cache'`.

- [ ] **Step 3: Write the minimal `CachingRewriter`**

Create `src/localmail/search/rewrite_cache.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_cache.py -k "hit_skips or distinct_queries" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/rewrite_cache.py tests/test_rewrite_cache.py
git commit -m "feat(search): CachingRewriter hit/miss core"
```

---

## Task 3: TTL expiry, date-keying, and LRU eviction

**Files:**
- Test: `tests/test_rewrite_cache.py`
- (No source change expected — these exercise behaviour already implemented in Task 2. If a test fails, fix `rewrite_cache.py`.)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rewrite_cache.py`:

```python
def test_ttl_expiry_recalls_inner():
    inner = FakeRewriter()
    clock = FakeClock()
    cache = CachingRewriter(
        inner, maxsize=128, ttl_s=100.0, today_provider=_const_today(), clock=clock
    )
    cache.rewrite("q")
    clock.t = 100.5  # past ttl_s
    cache.rewrite("q")
    assert inner.calls == ["q", "q"]  # re-called after expiry


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
    today["d"] = _date(2026, 6, 10)  # next day
    cache.rewrite("q")
    assert inner.calls == ["q", "q"]  # rolled over at midnight


def test_lru_evicts_least_recently_used():
    inner = FakeRewriter()
    cache = CachingRewriter(
        inner, maxsize=2, ttl_s=1200, today_provider=_const_today(), clock=FakeClock()
    )
    cache.rewrite("a")
    cache.rewrite("b")
    cache.rewrite("a")  # touch "a" so "b" is now LRU
    cache.rewrite("c")  # evicts "b"
    cache.rewrite("a")  # still cached -> no new inner call
    cache.rewrite("b")  # evicted -> inner called again
    assert inner.calls == ["a", "b", "c", "b"]
```

- [ ] **Step 2: Run tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_cache.py -k "ttl_expiry or date_is_part or lru_evicts" -v`
Expected: PASS (the Task 2 implementation already satisfies these).

- [ ] **Step 3: Commit**

```bash
git add tests/test_rewrite_cache.py
git commit -m "test(search): TTL, date-keying, LRU eviction for CachingRewriter"
```

---

## Task 4: Failures uncached, `maxsize=0` pass-through, delegation

**Files:**
- Test: `tests/test_rewrite_cache.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rewrite_cache.py`:

```python
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
    assert inner.calls == ["q", "q"]  # no negative caching


def test_maxsize_zero_is_pass_through():
    inner = FakeRewriter()
    cache = CachingRewriter(
        inner, maxsize=0, ttl_s=1200, today_provider=_const_today(), clock=FakeClock()
    )
    cache.rewrite("q")
    cache.rewrite("q")
    assert inner.calls == ["q", "q"]  # every call reaches inner
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
```

- [ ] **Step 2: Run tests**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_cache.py -k "failures_are_not_cached or maxsize_zero or name_model_and_close" -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_rewrite_cache.py
git commit -m "test(search): failures uncached, maxsize=0 pass-through, delegation"
```

---

## Task 5: Thread-safety smoke test

**Files:**
- Test: `tests/test_rewrite_cache.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rewrite_cache.py`:

```python
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
    assert all(r == results[0] for r in results)  # all see the same value
    # Best-effort: the hot key resolves to a single cached entry.
    assert len(cache._data) == 1
```

- [ ] **Step 2: Run test**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_cache.py::test_concurrent_hot_key_does_not_corrupt -v`
Expected: PASS (lock-guarded dict ops don't corrupt; concurrent cold misses converge on a single stored entry).

- [ ] **Step 3: Commit**

```bash
git add tests/test_rewrite_cache.py
git commit -m "test(search): thread-safety smoke for CachingRewriter"
```

---

## Task 6: Wire into `create_searcher`

**Files:**
- Modify: `src/localmail/search/__init__.py` (the `rewriter is _UNSET` block, lines ~91-102)
- Test: `tests/test_rewrite_cache.py`

- [ ] **Step 1: Write the failing wiring tests**

Append to `tests/test_rewrite_cache.py`:

```python
from unittest import mock

from localmail.config import LocalmailConfig
from localmail.search import create_searcher
from localmail.search.rewriter import OllamaLLMRewriter


def _cfg(*, cache_size: int) -> LocalmailConfig:
    cfg = LocalmailConfig()
    cfg.search.rewriter_enabled_by_default = True
    cfg.search.rewriter_cache_size = cache_size
    return cfg


def test_create_searcher_wraps_rewriter_when_cache_enabled():
    cfg = _cfg(cache_size=128)
    with mock.patch("localmail.search.open_pool"), \
         mock.patch("localmail.search.FastEmbedBackend"):
        searcher = create_searcher(cfg)
    assert isinstance(searcher._rewriter, CachingRewriter)
    assert isinstance(searcher._rewriter._inner, OllamaLLMRewriter)


def test_create_searcher_leaves_rewriter_bare_when_cache_disabled():
    cfg = _cfg(cache_size=0)
    with mock.patch("localmail.search.open_pool"), \
         mock.patch("localmail.search.FastEmbedBackend"):
        searcher = create_searcher(cfg)
    assert isinstance(searcher._rewriter, OllamaLLMRewriter)
```

> Note: `OllamaLLMRewriter.__init__` does not perform any IO (it only builds an
> `httpx.Client`), so no Ollama mock is needed. `open_pool` and `FastEmbedBackend`
> are patched only to avoid a DB connection and a model download during the
> construction test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_cache.py -k "create_searcher_wraps or create_searcher_leaves" -v`
Expected: FAIL — `test_create_searcher_wraps...` fails because `_rewriter` is a bare `OllamaLLMRewriter`, not a `CachingRewriter`.

- [ ] **Step 3: Add the wrap in `create_searcher`**

In `src/localmail/search/__init__.py`, change the rewriter construction block. Replace:

```python
    if rewriter is _UNSET:
        if cfg.search.rewriter_enabled_by_default:
            try:
                rewriter = OllamaLLMRewriter(cfg.search)
            except Exception as exc:
                logging.getLogger("localmail.search").warning(
                    "rewriter init failed (%s=%r): %s — continuing without --smart",
                    "rewriter_model", cfg.search.rewriter_model, exc,
                )
                rewriter = None
        else:
            rewriter = None
```

with:

```python
    if rewriter is _UNSET:
        if cfg.search.rewriter_enabled_by_default:
            try:
                rewriter = OllamaLLMRewriter(cfg.search)
            except Exception as exc:
                logging.getLogger("localmail.search").warning(
                    "rewriter init failed (%s=%r): %s — continuing without --smart",
                    "rewriter_model", cfg.search.rewriter_model, exc,
                )
                rewriter = None
            if rewriter is not None and cfg.search.rewriter_cache_size > 0:
                from localmail.search.rewrite_cache import CachingRewriter

                rewriter = CachingRewriter(
                    rewriter,
                    maxsize=cfg.search.rewriter_cache_size,
                    ttl_s=cfg.search.rewriter_cache_ttl_s,
                )
        else:
            rewriter = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_cache.py -k "create_searcher_wraps or create_searcher_leaves" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/__init__.py tests/test_rewrite_cache.py
git commit -m "feat(search): wrap rewriter in CachingRewriter in create_searcher"
```

---

## Task 7: Documentation

**Files:**
- Modify: `config.example.toml` (after the `# reranker_enabled = true` example in the `[search]` block, ~line 143)
- Modify: `README.md`

- [ ] **Step 1: Add the knobs to `config.example.toml`**

In `config.example.toml`, after the `# reranker_enabled = true` line (before the `# --- mcp server ---` divider), add:

```toml
# `--smart` query rewrites are memoised in a bounded per-process LRU+TTL cache
# keyed on (today, free_text), so repeated identical smart queries skip a fresh
# Ollama call. On by default; set rewriter_cache_size = 0 to disable.
# rewriter_cache_size = 128     # max cached rewrites; 0 disables
# rewriter_cache_ttl_s = 1200   # entry lifetime in seconds
```

- [ ] **Step 2: Add the knobs to `README.md`**

Locate the search/rewriter config documentation in `README.md`:

Run: `grep -n "rewriter_\|--smart\|reranker_enabled" README.md`

Add a short sentence + the two knobs alongside the existing `rewriter_*` documentation (match the surrounding format — table row or bullet). Example bullet form:

```markdown
- `rewriter_cache_size` (default `128`) / `rewriter_cache_ttl_s` (default `1200`):
  bounded per-process LRU+TTL cache for `--smart` rewrites, keyed on
  `(today, free_text)`. Repeated identical smart queries skip a fresh Ollama
  call. Set `rewriter_cache_size = 0` to disable.
```

If no `rewriter_*` documentation block exists in README, add the bullet to the search-configuration section near the `reranker_enabled` documentation.

- [ ] **Step 3: Commit**

```bash
git add config.example.toml README.md
git commit -m "docs(search): document rewriter cache knobs"
```

---

## Task 8: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the new test file**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_cache.py -v`
Expected: all tests PASS.

- [ ] **Step 2: Run the full suite**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ --deselect tests/test_daemon_control_socket.py`
Expected: previous count + new tests pass (was 1519 passed; now 1519 + new test count), 14 deselected.

- [ ] **Step 3: Type-check**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean (no new errors). Note: `CachingRewriter` structurally satisfies the `QueryRewriter` Protocol — `name`/`model` are properties (Protocol declares them as attributes; properties satisfy a read-only attribute Protocol). If mypy flags the `create_searcher` assignment, confirm the Protocol attributes are not `Final` and that `rewrite`'s signature matches exactly.

- [ ] **Step 4: Final commit (if any doc/lint touch-ups were needed)**

```bash
git add -A
git commit -m "chore(search): rewrite-result caching cleanup"
```

(Skip if nothing changed in Step 3.)

---

## Self-Review Notes

- **Spec coverage:** decorator (Task 2), date-keyed (Task 3), thread-safe lock (Tasks 2+5), failures uncached (Task 4), `maxsize=0` disable (Task 4), wiring (Task 6), config knobs (Task 1), docs (Task 7). All spec sections covered.
- **Type consistency:** `CachingRewriter(inner, *, maxsize, ttl_s, today_provider, clock)` used identically in every task; `_inner` / `_data` attribute names consistent across Tasks 2, 6, 4, 5.
- **No migration, no new dependency** — confirmed in spec.
