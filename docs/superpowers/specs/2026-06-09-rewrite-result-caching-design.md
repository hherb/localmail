# Rewrite-result caching (Phase 4 follow-up) — design

> **Status:** approved 2026-06-09. Small, self-contained Phase-4 follow-up.
> Adds a bounded, thread-safe, per-process LRU+TTL cache for `--smart` query
> rewrites so repeated identical smart queries skip a fresh Ollama call.

## Motivation

Every `--smart` search currently hits the Ollama `/api/generate` endpoint
fresh, even when the same free-text query was just rewritten. The rewrite is
deterministic for a given `(free_text, today, model)` (the rewriter runs at
`temperature=0`), so identical repeat queries within a short window do
redundant LLM work — adding latency on the hot path for no new information.

A bounded per-process LRU+TTL cache keyed on the rewrite input removes that
redundant call. Entries are tiny (`RewriteResult` = a short string, a small
list of terms, and a handful of scalar filters), so the cache can hold many
entries cheaply.

## Non-goals

- **Cross-process / shared cache.** localmail is single-host; a per-process
  cache is sufficient. No Redis, no DB-backed cache.
- **Caching failures.** Transient Ollama outages / model-loading errors must
  recover on the next call, so failures are never cached.
- **Changing rewrite semantics.** The cache is a pure latency optimisation;
  the `RewriteResult` returned on a hit is byte-for-byte what the inner
  rewriter would have produced.

## Component — `CachingRewriter` decorator

New module `src/localmail/search/rewrite_cache.py` with one class that wraps
any `QueryRewriter` and implements the same Protocol
(`name`, `model`, `rewrite(free_text) -> RewriteResult`):

```python
CachingRewriter(
    inner: QueryRewriter,
    *,
    maxsize: int,
    ttl_s: float,
    today_provider: Callable[[], date] = date.today,
    clock: Callable[[], float] = time.monotonic,
)
```

- **`name` / `model`** delegate to `inner`, so `Searcher.smart_available`
  (`self._rewriter is not None`) and the existing `rewrite_status` logic are
  unchanged — the decorator is transparent to the Searcher.
- **`rewrite(free_text)`**: compute the key; on a live hit (within TTL) return
  the cached `RewriteResult` without calling `inner`; on a miss call
  `inner.rewrite(free_text)`, store the result, and return it.
- **`close()`** forwards to `inner.close()` when the inner object exposes one
  (the production wrapped instance, `OllamaLLMRewriter`, does). Nothing in the
  production path currently calls `close()` on the searcher's rewriter, but
  forwarding keeps the decorator a faithful stand-in.

The Searcher and `OllamaLLMRewriter` are **untouched**.

## Cache key

`f"{today.isoformat()}\x00{free_text}"` — a single string key.

The date is part of the key because the rewrite output embeds **resolved
relative dates**: the prompt injects `today`, so "last week" becomes a concrete
`after` / `before` in `extracted_filters`. Without the date in the key, a
date-relative query cached just before midnight would return yesterday's
resolution after the day rolls over (up to a full TTL of staleness). Keying on
the date rolls the cache over cleanly at midnight.

`model` and `rewriter_max_expansion_terms` are **not** in the key: they are
fixed for the life of the wrapped instance (read from `cfg` at construction),
so every entry in a given `CachingRewriter` already shares them. A config
change requires a process restart, which builds a fresh cache.

The `\x00` separator is a NUL byte, which cannot appear in `free_text` (the
parser strips NUL from all text and `free_text` originates from user query
strings), so the `(today, free_text)` pair maps to the key unambiguously.

## Internals — bounded LRU + TTL, thread-safe

An `OrderedDict[str, tuple[float, RewriteResult]]` (insertion-stamp + value)
guarded by a `threading.Lock`. Same eviction shape as
[page_cache.py](../../../src/localmail/search/page_cache.py) (`move_to_end` on
access, `popitem(last=False)` past `maxsize`, TTL check on read), **plus a
lock**.

The lock is the one deliberate divergence from `PageCache` (which is
documented as thread-unsafe): the rewriter is shared across **concurrent MCP
requests** (`FastMCP(stateless_http=True, json_response=True)` mounts one
app-level `Searcher` on `app.state.searcher`; concurrent tool calls share its
one `_rewriter`). That is exactly the concurrency `page_cache.py`'s docstring
flagged as a future concern. The lock only guards in-memory dict operations, so
its cost is negligible; the inner `rewrite()` (the slow Ollama call) runs
**outside** the lock so a cache miss never blocks a concurrent hit.

Concurrency contract: two concurrent misses for the same hot key may each call
`inner.rewrite()` (we do not hold the lock across the inner call — that would
serialise all rewrites). This is acceptable: the rewrite is idempotent and the
second store simply overwrites with an identical value. We optimise for not
blocking, not for de-duplicating simultaneous cold misses.

## Disable path

`maxsize <= 0` makes `rewrite()` a pure pass-through to `inner` — no dict
allocation, no lock acquisition. This is the config "off" switch and the
zero-overhead default for anyone who sets `rewriter_cache_size = 0`.

## Wiring

In [create_searcher](../../../src/localmail/search/__init__.py): after the
existing block builds `OllamaLLMRewriter`, wrap it when a rewriter was actually
built and caching is enabled:

```python
if rewriter is not None and cfg.search.rewriter_cache_size > 0:
    rewriter = CachingRewriter(
        rewriter,
        maxsize=cfg.search.rewriter_cache_size,
        ttl_s=cfg.search.rewriter_cache_ttl_s,
    )
```

This sits inside the `rewriter is _UNSET` branch (the default-construction
path). An explicitly injected `rewriter=` is left unwrapped — a caller passing
their own rewriter owns its caching policy. The `Searcher` constructor and
signature are unchanged.

## Config — two new `SearchConfig` knobs (no magic numbers)

Added to `SearchConfig` in `src/localmail/config.py`, next to the existing
`# --- query rewriter (Phase 4) ---` block:

- `rewriter_cache_size: int = 128` — max entries. Entries are tiny, so this is
  larger than `page_cache_size` (16). `0` disables the cache entirely.
- `rewriter_cache_ttl_s: int = 1200` — entry lifetime in seconds; mirrors
  `page_cache_ttl_s`.

`config.example.toml` and the README config table get the two new keys.

## Testing (TDD)

### Unit — `tests/test_rewrite_cache.py`

A `FakeRewriter` implementing `QueryRewriter` that records call counts and
returns a deterministic `RewriteResult`, plus injected `clock` and
`today_provider`:

1. **Hit skips inner** — two identical `rewrite()` calls → one inner call.
2. **TTL expiry re-calls** — advance the injected clock past `ttl_s` → second
   call hits inner again.
3. **Date in key** — same `free_text`, different `today` → two inner calls.
4. **LRU eviction** — fill past `maxsize`, the least-recently-used key is
   evicted (re-querying it re-calls inner); a recently-touched key survives.
5. **Failures are not cached** — inner raises `httpx.HTTPError` /
   `RewriteParseError` → propagates, and a second call calls inner again
   (no negative caching).
6. **`maxsize=0` pass-through** — every call reaches inner; no caching.
7. **Delegation** — `name`, `model` mirror inner; `close()` forwards (spy).
8. **Thread-safety smoke** — N threads issuing the same hot key concurrently
   do not raise and converge on the cached value (best-effort; asserts no
   corruption / no exception, not a single inner call).

### Wiring — `tests/test_create_searcher.py` (or extend existing)

`create_searcher` with `rewriter_enabled_by_default=True` and
`rewriter_cache_size > 0` produces a `Searcher` whose `_rewriter` is a
`CachingRewriter` wrapping an `OllamaLLMRewriter`; with `rewriter_cache_size = 0`
the rewriter is the bare `OllamaLLMRewriter`. (Construction only — no Ollama
call.)

## Acceptance

- A repeated identical `--smart` query shows near-zero `rewrite` timing on the
  2nd call (cache hit; no Ollama round-trip).
- The cache is bounded (`rewriter_cache_size`) and entries expire
  (`rewriter_cache_ttl_s`).
- Setting `rewriter_cache_size = 0` restores the un-cached behaviour exactly.
- mypy clean; full suite green.

## Files touched

- `src/localmail/search/rewrite_cache.py` — new (`CachingRewriter`).
- `src/localmail/search/__init__.py` — wrap in `create_searcher`.
- `src/localmail/config.py` — two new `SearchConfig` fields.
- `config.example.toml`, `README.md` — document the knobs.
- `tests/test_rewrite_cache.py` — new unit tests.
- wiring test for `create_searcher`.

No migration (in-memory only). No new dependency.
