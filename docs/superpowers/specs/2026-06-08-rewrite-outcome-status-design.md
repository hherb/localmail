# Design — structured rewrite outcome on the search response (#176)

> **Status:** approved 2026-06-08. Closes the structural ambiguity in
> [#176](https://github.com/hherb/localmail/issues/176) and subsumes the
> carried "opaque rewrite-failure signal" follow-up. No migration, no new
> config.

## Problem

The smart query rewriter (Phase 4) surfaces exactly one wire signal today:
`rewrite_skipped: bool` on every search response. It conflates distinct
outcomes:

- On a **continuation/keyset page** with `smart=true`, `run_search` correctly
  does *not* re-rewrite (the cached enriched parse is reused) and reports
  `rewrite_skipped: false`. A naive consumer reads that as "smart was applied
  on this page" when in fact `smart` was ignored — the page-1-only design is
  invisible on the wire (#176).
- When a page-1 rewrite *is* skipped, the bool says nothing about **why**
  (rewriter not configured, model not pulled, service unreachable, unparseable
  response). The actionable detail is logged server-side and discarded before
  the wire.

## Goals

1. Make the per-page rewrite outcome **honest and machine-readable**.
2. Tell the user **why** a rewrite did not apply, with an **actionable**
   message where one exists (e.g. which `ollama pull` fixes it).
3. **Do not break** the existing `rewrite_skipped` contract (GUI type, MCP
   docs, tests, the CLI note).
4. No raw exception text on the wire — curated messages only (remote MCP
   agents must not receive internal URLs/hostnames/stack fragments).

## Non-goals

- No change to *when* the rewrite runs (still page-1-only). This is a
  reporting change, not a behaviour change.
- No caching of rewrite results (separate carried follow-up).
- No non-Ollama rewriter backend.

## Wire shape

Every search response — `POST /v1/search`, the MCP `search` tool, and the
shared `api/search.py::run_search` dict — gains two flat fields alongside the
retained bool. (Flat fields mirror the existing `rewrite_skipped` style; no
nested object.)

| field | type | meaning |
|---|---|---|
| `rewrite_skipped` | `bool` | **kept**, now *derived* = `status ∈ {unavailable, failed}` |
| `rewrite_status` | `str` | always present; one of the five values below |
| `rewrite_note` | `str \| null` | curated, actionable detail; `null` when nothing useful to say |

### `rewrite_status` values

| value | when | note |
|---|---|---|
| `applied` | page 1, rewriter configured, rewrite ran and was applied | `null` |
| `unavailable` | `smart=true` requested but no rewriter configured on this server | `"smart search is not configured on this server"` |
| `failed` | rewrite **attempted** on page 1 but errored | curated cause (see below) |
| `not_attempted` | `smart=true` on a **continuation/keyset page** (page-1-only signal) | `"smart query rewriting applies to the first page only; this is a continuation page"` |
| `not_requested` | `smart=false`, **or** the empty-ACL short-circuit | `null` |

`rewrite_skipped` is therefore `true` only for `unavailable` and `failed`.
This preserves today's behaviour exactly: continuation pages stay
`rewrite_skipped: false` (now also carrying `not_attempted`), and the empty-ACL
short-circuit stays `false`.

### Curated `failed` notes

A pure classifier maps each typed failure raised by `OllamaLLMRewriter` to a
fixed message. The configured model name (already user-known) is the only
interpolated runtime value.

| failure | detection | `rewrite_note` |
|---|---|---|
| model not pulled | `httpx.HTTPStatusError` with `status_code == http.HTTPStatus.NOT_FOUND` | `rewriter model '<model>' is not available; pull it with: ollama pull <model>` |
| service unreachable | any other `httpx.HTTPError` (connect, timeout, non-404 status) | `could not reach the rewriter service` |
| unparseable response | `RewriteParseError` (incl. 200-with-missing-`response`-key) | `the rewriter returned an unparseable response` |

The full `str(exc)` continues to be logged at WARNING server-side; only the
curated note travels on the wire.

## Components

### 1. New pure module `src/localmail/search/rewrite_status.py`

Keeps `rewriter.py` focused (no growth toward the 500-line guideline). Pure —
no IO, no FastAPI; reusable by the api/ layer and any future transport.

- `RewriteStatus` — a `Literal["applied", "unavailable", "failed",
  "not_attempted", "not_requested"]` plus module constants
  (`APPLIED`, `UNAVAILABLE`, `FAILED`, `NOT_ATTEMPTED`, `NOT_REQUESTED`).
- Curated note string constants (`NOTE_UNAVAILABLE`, `NOTE_NOT_ATTEMPTED`,
  `NOTE_UNREACHABLE`, `NOTE_UNPARSEABLE`, and a `note_model_unavailable(model)`
  builder for the interpolated case).
- `classify_rewrite_failure(exc, *, model) -> str` — exception → curated note.
  Uses `http.HTTPStatus.NOT_FOUND` (no magic number).
- `rewrite_skipped_for_status(status) -> bool` — the derived back-compat bool
  (`status in {UNAVAILABLE, FAILED}`).

### 2. `SearchPage` (searcher.py)

Replace the `rewrite_skipped: bool` field with:
- `rewrite_status: str = NOT_REQUESTED`
- `rewrite_note: str | None = None`

The dataclass no longer carries the derived bool — it is computed downstream
from `rewrite_status`.

### 3. `Searcher.search`

After the existing rewrite `try/except` block (which already runs **before**
both the lexical-date and hybrid page-1 branches), compute the page-1 outcome
once:

- not (`smart` and non-empty free text) → `(NOT_REQUESTED, None)`
- exception caught → `(FAILED, classify_rewrite_failure(exc, model=cfg.rewriter_model))`
- otherwise → `(APPLIED, None)`

Pass `rewrite_status=` / `rewrite_note=` into **both** page-1 `SearchPage`
constructions (hybrid path and lexical-date path). `continue_page` /
`grow_pool` SearchPages keep the defaults — the api/ layer overrides them on
the cursor branch, so the value there is moot.

The caught-exception reference must be captured for the classifier (assign in
the `except` and read after the block, or classify inside the `except`).

### 4. `api/search.py::run_search`

Owns the layer-specific statuses it alone knows, and derives the bool:

- **Empty-ACL short-circuit** → `(NOT_REQUESTED, None)`, `rewrite_skipped=False`.
- **Page-1 branch** (`cursor is None`):
  - `smart and not searcher.smart_available` → `(UNAVAILABLE, NOTE_UNAVAILABLE)`
  - else → `(page.rewrite_status, page.rewrite_note)` (applied / failed /
    not_requested as set by `Searcher.search`)
- **Continuation/keyset branch** (`cursor is not None`):
  - `smart` → `(NOT_ATTEMPTED, NOTE_NOT_ATTEMPTED)` ← the core #176 fix
  - else → `(NOT_REQUESTED, None)`
- Final `rewrite_skipped = rewrite_skipped_for_status(status)`.

The response dict gains `rewrite_status` and `rewrite_note`; `rewrite_skipped`
stays present and is now derived.

### 5. Transports (no code change beyond what propagates)

`POST /v1/search` returns `dict[str, Any]` with no response model, and the MCP
`tool_search` returns the `run_search` dict directly — both new fields
propagate automatically. The MCP `search` tool docstring and `docs/mcp-usage.md`
gain a sentence describing `rewrite_status` / `rewrite_note`.

### 6. CLI (`cli.py`)

The `localmail search --smart` note path already branches on
`page.rewrite_skipped`. Rewire it to print `page.rewrite_note` when present
(richer, actionable message) and fall back to the existing generic line
otherwise. The CLI keeps its interactive hard-error for an explicitly
unavailable rewriter (unchanged).

### 7. GUI type (`gui/src/lib/api/search.ts`)

Add `rewrite_status: string` and `rewrite_note: string | null` to the
`SearchResponse` interface so the type stays honest with the wire. The GUI does
not consume them; existing `.test.ts` fixtures need the new fields only if the
type is constructed strictly (verify and add where the compiler requires).

## Data flow

```
OllamaLLMRewriter.rewrite ──(typed exc)──▶ Searcher.search except-block
                                              │  classify_rewrite_failure(exc, model)
                                              ▼
                              SearchPage{rewrite_status, rewrite_note}   (page 1)
                                              │
run_search ── overrides for unavailable / not_attempted / ACL ──▶ final status
            └── rewrite_skipped_for_status(status) ──▶ rewrite_skipped (bool)
                                              ▼
                       {results, …, rewrite_skipped, rewrite_status, rewrite_note}
```

The exception never leaves `Searcher`; only the curated note string travels.

## Error handling / exposure

Curated messages only. The single interpolated runtime value is the configured
model name (`cfg.rewriter_model`), which the operator already knows. No URLs,
hostnames, ports, or stack fragments reach the wire. Full `str(exc)` stays in
the WARNING log.

## Testing (TDD)

**Pure unit tests — `tests/test_rewrite_status.py` (new):**
- `classify_rewrite_failure`: a 404 `HTTPStatusError` → model-pull note (asserts
  the model name is interpolated); a connect/timeout/non-404 `HTTPError` →
  unreachable note; a `RewriteParseError` → unparseable note.
- `rewrite_skipped_for_status`: `True` for `unavailable`/`failed`, `False` for
  the other three.

**`run_search` tests — `tests/test_api_search.py` (extend):**
Assert `(rewrite_status, rewrite_note, rewrite_skipped)` together for each of:
- page-1 applied, page-1 failed (+note), page-1 `smart` & not available →
  `unavailable`,
- **continuation cursor + `smart=true` → `not_attempted`** (the #176 acceptance;
  `rewrite_skipped is False`),
- continuation cursor + `smart=false` → `not_requested`,
- empty-ACL short-circuit → `not_requested`, `rewrite_skipped is False`
  (extends the existing `total_estimate`-shape test).

**Searcher tests — `tests/test_searcher_smart.py` (or existing smart test
file, extend):**
- page-1 hybrid: inject a fake rewriter that succeeds → `applied`; that raises a
  404 `HTTPStatusError` → `failed` + model-pull note.
- lexical-date page-1 (`sort="date"`, non-empty query) carries the same status.

**MCP — `tests/test_mcp_tools.py` (extend):** the empty-grants test already
asserts the response dict; add `rewrite_status`/`rewrite_note` to its expected
shape.

**CLI — existing `--smart` note test (extend):** a `failed` page prints the
curated note.

## Open / accepted

- The empty-ACL short-circuit reports `not_requested` even when `smart=true`
  was requested — the query short-circuited before any page-1 rewrite, so no
  rewrite was performed; `not_requested` (note `null`) is the honest value.
  Documented above.
- GUI fields added for type honesty only; no GUI behaviour change.
