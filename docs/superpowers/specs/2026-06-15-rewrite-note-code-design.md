# Machine-readable `rewrite_note_code` — design

> **Status: approved 2026-06-15.** A small, additive follow-up to the structured
> rewrite-outcome work (#176, #175 — see
> [2026-06-08-rewrite-outcome-status-design.md](2026-06-08-rewrite-outcome-status-design.md)).
> No migration, no new dependency.

## Problem

Every search response already carries `rewrite_status` (a 5-value enum) and an
optional curated `rewrite_note` (a human-readable string). For the `failed`
status the note can be one of **three** distinct causes — model not pulled,
service unreachable, unparseable response. A machine consumer (an MCP agent, the
GUI) that wants to *act differently* per cause currently has only the
human note to switch on:

- The note text is curated for humans and may be reworded at any time.
- The "model not pulled" note **interpolates the model name**, so even exact
  string matching is impossible without reconstructing the template.

So today's wire is honest for humans but not *switchable* for machines.

## Goal

Add a stable, machine-readable **`rewrite_note_code`** to every search response.
It is present for **every note-bearing state** (1:1 with the curated note) and
`null` exactly when the note is `null`.

Non-goals (deliberately out of scope, YAGNI): changing `rewrite_status`,
changing `rewrite_skipped`, adding a migration, touching the CLI's human-facing
output, or a cloud rewriter backend.

## Wire shape (purely additive)

A new field on `/v1/search`, the MCP `search` tool, and `SearchPage`:

| field | type | meaning |
|---|---|---|
| `rewrite_note_code` | `str \| null` | machine partner of `rewrite_note`; `null` when the note is `null` |

Existing fields (`rewrite_status`, `rewrite_note`, `rewrite_skipped`,
`total_estimate`, …) are unchanged.

### Code ↔ status ↔ note mapping

| `rewrite_status` | `rewrite_note_code` | `rewrite_note` (rendered from code) |
|---|---|---|
| `applied` | `null` | `null` |
| `not_requested` | `null` | `null` |
| `unavailable` | `not_configured` | `smart search is not configured on this server` |
| `not_attempted` | `continuation_page` | `smart query rewriting applies to the first page only; this is a continuation page` |
| `failed` | `missing_model` | `rewriter model '<model>' is not available; pull it with: ollama pull <model>` |
| `failed` | `unreachable` | `could not reach the rewriter service` |
| `failed` | `unparseable` | `the rewriter returned an unparseable response` |

The `failed` failure detection is unchanged from #176:

| failure | detection | code |
|---|---|---|
| model not pulled | `httpx.HTTPStatusError` with `status_code == http.HTTPStatus.NOT_FOUND` | `missing_model` |
| service unreachable | any other `httpx.HTTPError` | `unreachable` |
| unparseable response | `RewriteParseError` | `unparseable` |

## Design principle: the code is canonical, the note is rendered from it

There is exactly **one** source of truth. Each curated note is produced *from*
its code via a single pure renderer, so the code and the note can never drift.
The only runtime value a note needs is the configured model name (already
user-known), used solely by the `missing_model` rendering.

## Components

### 1. `src/localmail/search/rewrite_status.py` (pure; the only changed module here)

- New `RewriteNoteCode` `Literal["missing_model", "unreachable", "unparseable",
  "not_configured", "continuation_page"]` plus the five matching constants
  (`MISSING_MODEL`, `UNREACHABLE`, `UNPARSEABLE`, `NOT_CONFIGURED`,
  `CONTINUATION_PAGE`).
- **`classify_rewrite_failure(exc) -> RewriteNoteCode`** — signature change:
  returns a **code** (not a note) and **drops** the `model` kwarg (model is only
  needed at render time, not at classification time). Detection logic is
  byte-for-byte the same as #176; only the return value changes.
- **`note_for_code(code, *, model=None) -> str`** — the single renderer.
  `missing_model` returns `note_model_unavailable(model)` (raises if `model is
  None`, since that code is meaningless without one); the other four return
  their existing static `NOTE_*` constant. Total over the `RewriteNoteCode`
  Literal.
- The existing note constants (`NOTE_UNAVAILABLE`, `NOTE_NOT_ATTEMPTED`,
  `NOTE_UNREACHABLE`, `NOTE_UNPARSEABLE`) and `note_model_unavailable(model)`
  stay — they become `note_for_code`'s lookup table / branches.
- `rewrite_skipped_for_status` unchanged.

### 2. `SearchPage` (searcher.py)

Add `rewrite_note_code: str | None = None`. Computed once in the page-1 outcome
block: on failure, `code = classify_rewrite_failure(exc)` then `note =
note_for_code(code, model=cfg.rewriter_model)`. The code is threaded into all
three `SearchPage` construction sites alongside the existing status/note.

### 3. `api/search.py::run_search`

Compute `(status, note, code)` together per branch, rendering each note via
`note_for_code(code, …)` so the api-layer notes (`unavailable`,
`not_attempted`) also flow from their codes:

- empty-ACL short-circuit → `not_requested`, note `None`, code `None`.
- `cursor is None`:
  - `smart and not searcher.smart_available` → `unavailable`,
    code `not_configured`, note `note_for_code(NOT_CONFIGURED)`.
  - else → pass through `page.rewrite_status` / `page.rewrite_note` /
    `page.rewrite_note_code`.
- cursor present:
  - `smart` → `not_attempted`, code `continuation_page`,
    note `note_for_code(CONTINUATION_PAGE)`.
  - else → `not_requested`, note `None`, code `None`.

Add `"rewrite_note_code": code` to the returned dict (including the empty-ACL
short-circuit, which returns `None`).

### 4. Docstrings

Update the MCP `search` tool docstring ([mcp/tools.py](../../../src/localmail/mcp/tools.py)),
the MCP server tool description ([mcp/server.py](../../../src/localmail/mcp/server.py)),
and the HTTP route/`run_search` docstring ([api/search.py](../../../src/localmail/api/search.py))
to name the new field. The CLI is human-facing (prints the note) — **no change**.

## Testing (TDD — tests first)

- **`tests/test_rewrite_status.py`** (extend): `classify_rewrite_failure`
  returns each code for the right exception; `note_for_code` renders every code
  (incl. model interpolation for `missing_model` and the `model is None` guard);
  totality — every `RewriteNoteCode` value renders a non-empty note.
- **Searcher**: `SearchPage.rewrite_note_code` is set to the right code on a
  page-1 rewrite failure and `None` on `applied` / non-smart.
- **`api/search`**: the response dict carries the correct `rewrite_note_code`
  for each branch — `not_configured` (unavailable), `continuation_page`
  (continuation), `None` (applied / not_requested / empty-ACL), and the three
  `failed` codes.
- **Serve + MCP wire-presence**: `rewrite_note_code` is present (default `None`)
  on the search route and the MCP tool responses, mirroring the existing
  `rewrite_status` wire-presence tests.

## Risks

- **Signature break of `classify_rewrite_failure`** — internal pure helper, two
  call sites (searcher + tests). Updated in this change; no external consumer.
- **Code/status redundancy** for `not_configured` / `continuation_page` (the
  status already implies them). Accepted deliberately: a uniform
  "switch on one field" experience for machine clients, with `null` only when
  there is genuinely nothing to say.
