# Design: `--smart` query rewriter over MCP + HTTP

> **Date:** 2026-06-08 · **Status:** approved, ready for plan · **Migration:** none · **Config:** none new

## Problem

The Phase-4 LLM query rewriter (`--smart`) is wired into the Python `Searcher`
and the `localmail search` CLI only. The two network read surfaces — the HTTP
`POST /v1/search` endpoint and the MCP `search` tool — cannot request a smart
rewrite, and neither surfaces the `rewrite_skipped` signal that tells a caller
the rewrite was attempted but fell through to the original query.

This is the §1 "MCP / HTTP `smart=` param" follow-up from the 2026-06-07
handoff. **Acceptance (from the handoff):** MCP `search(smart=true)` runs the
rewriter and a response field reflects `rewrite_skipped`.

## Background: how `smart` works today

- `Searcher.search(..., smart: bool = False)` runs the rewriter on page 1 only.
  Continuation (`continue_page`) and `grow_pool` reuse the cached enriched
  `parsed` and never re-rewrite — `rewrite_skipped` is a **page-1 signal**.
- `SearchPage.rewrite_skipped: bool` carries the outcome. It is set `True` when
  the rewrite *call* fails (`httpx.HTTPError`, `RewriteParseError`) — search
  still runs on the un-rewritten query (no-silent-failure: the CLI prints a
  `note:`).
- A **distinct** failure mode: when `smart=True` but **no rewriter is
  configured** (`rewriter_enabled_by_default=false`, or `OllamaLLMRewriter`
  init failed in `create_searcher`), `Searcher.search` raises `RuntimeError`
  before retrieval. The CLI catches it and exits non-zero (interactive UX).
- `run_search` in `api/search.py` is the single shared entry point for both the
  HTTP route and the MCP tool. It has no `smart` parameter today; its response
  is `{results, next_cursor, total_estimate, took_ms}`.

## Decisions

### D1 — No-rewriter behaviour on the wire: **graceful**

When a wire client passes `smart=true` to a server with no rewriter configured,
the request **does not hard-fail**. The un-rewritten query runs and the response
carries `rewrite_skipped=true` — the same signal as a failed rewrite call. This
keeps search robust for agents/GUIs talking to a server that simply lacks an
Ollama backend. (The CLI keeps its current hard-error behaviour; it is
interactive and a missing local rewriter is a config error worth surfacing
loudly there.)

### D2 — Detection via a public property, not exception-as-control-flow

Add a read-only `Searcher.smart_available` property (`self._rewriter is not
None`). `run_search` computes `effective_smart = smart and
searcher.smart_available`, so the `RuntimeError` guard is never triggered and no
double search occurs. This respects the #71 convention that the `api/` layer
uses public `Searcher` accessors and never reaches into `searcher._rewriter`.

### D3 — `rewrite_skipped` is always present on the wire

The search response gains a stable `rewrite_skipped: bool` field (default
`False`), present on every response regardless of `smart`. A stable wire shape
is simpler for consumers than a conditionally-present field.

## Components

| File | Change |
|------|--------|
| `src/localmail/search/searcher.py` | New read-only property `smart_available -> bool` returning `self._rewriter is not None`. |
| `src/localmail/api/search.py` | `run_search` gains `smart: bool = False`. Pass `smart=(smart and searcher.smart_available)` **only** on the page-1 branch (`cursor is None`). Add `rewrite_skipped` to the response. |
| `src/localmail/serve/routes/search.py` | `SearchRequest` gains `smart: bool = False`; thread to `run_search`. |
| `src/localmail/mcp/tools.py` | `tool_search` gains `smart: bool = False`; thread to `run_search`. |
| `src/localmail/mcp/server.py` | `search` tool gains a `smart` Annotated param with an agent-facing description; thread to `tool_search`. |

### `run_search` logic

```text
effective_smart = smart and searcher.smart_available
rewrite_unavailable = (cursor is None) and smart and not searcher.smart_available

if cursor is None:
    page = searcher.search(query, ..., smart=effective_smart)
elif is_keyset_cursor(cursor):
    page = searcher.search(query, ..., keyset_cursor=...)   # no smart — continuation
else:
    page = _continue_or_grow(...)                            # no smart — continuation

rewrite_skipped = rewrite_unavailable or getattr(page, "rewrite_skipped", False)
response["rewrite_skipped"] = rewrite_skipped
```

Continuation pages (`cursor` present) never re-rewrite and report
`rewrite_skipped=false` — both because `page.rewrite_skipped` defaults `False`
and because `rewrite_unavailable` is gated on `cursor is None`.

## Wire contract

```jsonc
// POST /v1/search response (and MCP search tool result)
{
  "results": [ ... ],
  "next_cursor": "…" | null,
  "total_estimate": null,
  "took_ms": 12.3,
  "rewrite_skipped": false   // NEW — always present
}
```

Request gains an optional `smart` (HTTP body field / MCP tool param), default
`false`.

## Testing (TDD — red first)

- `Searcher.smart_available`: `True` when a rewriter is wired, `False` when `None`.
- `run_search`:
  - `smart=true` + rewriter present → forwards `smart=True`; response
    `rewrite_skipped` mirrors `page.rewrite_skipped`.
  - `smart=true` + **no** rewriter → `rewrite_skipped=true` **and results still
    returned** (graceful; underlying search ran un-rewritten).
  - `smart=true` on a **continuation cursor** → no re-rewrite,
    `rewrite_skipped=false`.
  - `smart=false` (default) → `effective_smart` never set; `rewrite_skipped=false`.
- HTTP `POST /v1/search`: `smart` body field threads to `run_search`; response
  carries `rewrite_skipped`. Keep the existing wire-shape invariants
  (`date = COALESCE(...)`) green.
- MCP `tool_search` (+ `server.py` search tool): `smart` param threads through.

## Out of scope (deferred, tracked in NEXT_SESSION §1)

- Rewrite-result caching (per-process LRU).
- Actionable missing-model note / pre-flight probe.
- Non-Ollama rewriter backends.

## Migration / config

None. `httpx` is already a dependency; the rewriter is an external HTTP service.
No new `[search]` config keys.
