# Structured Rewrite Outcome (rewrite_status / rewrite_note) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single ambiguous `rewrite_skipped: bool` search-response signal with a structured `rewrite_status` (5-value enum) + curated `rewrite_note`, while keeping `rewrite_skipped` as a derived back-compat field.

**Architecture:** A new pure module `search/rewrite_status.py` holds the status constants, the exception→note classifier, and the derived-bool helper. `SearchPage` carries `rewrite_status`/`rewrite_note` (replacing the `rewrite_skipped` field). `Searcher.search` classifies its own page-1 outcome; `api/search.py::run_search` owns the layer-specific statuses (`unavailable`, `not_attempted`, ACL short-circuit) and derives `rewrite_skipped`. Both transports (`/v1/search`, MCP `search`) return the `run_search` dict unchanged, so the new fields propagate automatically.

**Tech Stack:** Python 3.12, psycopg, FastAPI, pytest, mypy; `httpx` exceptions; the existing Phase-4 rewriter.

**Spec:** [docs/superpowers/specs/2026-06-08-rewrite-outcome-status-design.md](../specs/2026-06-08-rewrite-outcome-status-design.md)

**Run tests with:** `unset VIRTUAL_ENV && uv run --extra mcp pytest …` (the `--extra mcp` is needed for the MCP tests; deselect `tests/test_daemon_control_socket.py` on macOS — pre-existing `AF_UNIX path too long`).

---

## File Structure

- **Create:** `src/localmail/search/rewrite_status.py` — pure status constants, `classify_rewrite_failure`, `rewrite_skipped_for_status`, note constants/builders. No IO.
- **Create:** `tests/test_rewrite_status.py` — pure unit tests for the module.
- **Modify:** `src/localmail/search/searcher.py` — `SearchPage` field rename (`rewrite_skipped` → `rewrite_status` + `rewrite_note`); `Searcher.search` computes the page-1 outcome and passes it to the 3 page-1 `SearchPage` constructions.
- **Modify:** `src/localmail/api/search.py` — `run_search` final status logic + derived `rewrite_skipped`; response dict gains `rewrite_status`/`rewrite_note`.
- **Modify:** `tests/test_searcher_smart.py` — update assertions from the bool to the new fields; add a 404→model-pull-note test.
- **Modify:** `tests/test_api_search.py` — update `_fake_searcher_for_smart` + existing smart tests; add status-matrix tests (incl. continuation → `not_attempted`).
- **Modify:** `tests/test_mcp_tools.py` — extend the empty-grants expected dict with the new fields.
- **Modify:** `src/localmail/cli.py` — print `page.rewrite_note` when present.
- **Modify:** `tests/test_cli_search.py` — assert the curated note prints (if a matching test exists; else add one).
- **Modify:** `src/localmail/mcp/tools.py` + `docs/mcp-usage.md` — docstring/doc sentence for the new fields.
- **Modify:** `gui/src/lib/api/search.ts` — add the two fields (optional) to `SearchResponse`.

---

## Task 1: Pure `rewrite_status` module

**Files:**
- Create: `src/localmail/search/rewrite_status.py`
- Test: `tests/test_rewrite_status.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rewrite_status.py`:

```python
"""Unit tests for the pure rewrite-outcome status/note helpers."""
import httpx
import pytest

from localmail.search.rewrite_status import (
    APPLIED,
    FAILED,
    NOT_ATTEMPTED,
    NOT_REQUESTED,
    UNAVAILABLE,
    classify_rewrite_failure,
    rewrite_skipped_for_status,
)
from localmail.search.rewriter import RewriteParseError


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://localhost:11434/api/generate")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


def test_classify_404_returns_actionable_model_pull_note():
    note = classify_rewrite_failure(_status_error(404), model="granite4.1:3b-q8_0")
    assert "granite4.1:3b-q8_0" in note
    assert "ollama pull granite4.1:3b-q8_0" in note


def test_classify_non_404_status_returns_unreachable_note():
    note = classify_rewrite_failure(_status_error(500), model="m")
    assert note == "could not reach the rewriter service"


def test_classify_connect_error_returns_unreachable_note():
    note = classify_rewrite_failure(httpx.ConnectError("down"), model="m")
    assert note == "could not reach the rewriter service"


def test_classify_parse_error_returns_unparseable_note():
    note = classify_rewrite_failure(RewriteParseError("bad"), model="m")
    assert note == "the rewriter returned an unparseable response"


@pytest.mark.parametrize(
    "status,expected",
    [
        (UNAVAILABLE, True),
        (FAILED, True),
        (APPLIED, False),
        (NOT_ATTEMPTED, False),
        (NOT_REQUESTED, False),
    ],
)
def test_rewrite_skipped_for_status(status, expected):
    assert rewrite_skipped_for_status(status) is expected
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_status.py -q`
Expected: FAIL — `ModuleNotFoundError: localmail.search.rewrite_status`.

- [ ] **Step 3: Write the module**

Create `src/localmail/search/rewrite_status.py`:

```python
"""Pure helpers describing the per-page outcome of a smart query rewrite.

No IO, no FastAPI — reusable by the api/ layer and any future transport.
The wire carries a ``rewrite_status`` (one of the constants below) plus an
optional curated ``rewrite_note``; raw exception text never leaves the
``Searcher`` (only these curated strings travel).
"""
from __future__ import annotations

from http import HTTPStatus
from typing import Literal

import httpx

from localmail.search.rewriter import RewriteParseError

RewriteStatus = Literal[
    "applied", "unavailable", "failed", "not_attempted", "not_requested"
]

APPLIED: RewriteStatus = "applied"
UNAVAILABLE: RewriteStatus = "unavailable"
FAILED: RewriteStatus = "failed"
NOT_ATTEMPTED: RewriteStatus = "not_attempted"
NOT_REQUESTED: RewriteStatus = "not_requested"

NOTE_UNAVAILABLE = "smart search is not configured on this server"
NOTE_NOT_ATTEMPTED = (
    "smart query rewriting applies to the first page only; "
    "this is a continuation page"
)
NOTE_UNREACHABLE = "could not reach the rewriter service"
NOTE_UNPARSEABLE = "the rewriter returned an unparseable response"

_SKIPPED_STATUSES: frozenset[str] = frozenset({UNAVAILABLE, FAILED})


def note_model_unavailable(model: str) -> str:
    """Actionable note for an Ollama 'model not pulled' (404) failure."""
    return (
        f"rewriter model {model!r} is not available; "
        f"pull it with: ollama pull {model}"
    )


def classify_rewrite_failure(exc: Exception, *, model: str) -> str:
    """Map a typed rewriter failure to a curated, actionable note."""
    if (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == HTTPStatus.NOT_FOUND
    ):
        return note_model_unavailable(model)
    if isinstance(exc, RewriteParseError):
        return NOTE_UNPARSEABLE
    return NOTE_UNREACHABLE


def rewrite_skipped_for_status(status: str) -> bool:
    """Back-compat bool: a rewrite was *skipped* only when unavailable/failed."""
    return status in _SKIPPED_STATUSES
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_status.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/rewrite_status.py tests/test_rewrite_status.py
git commit -m "feat(search): pure rewrite-outcome status + curated-note module (#176)"
```

---

## Task 2: SearchPage + Searcher + run_search produce the structured outcome

This is the atomic core: the `SearchPage.rewrite_skipped` field is renamed, so every producer (`Searcher.search`) and consumer (`run_search`, the test fakes) must change together to keep the suite green.

**Files:**
- Modify: `src/localmail/search/searcher.py` (`SearchPage` ~line 252; rewrite block ~891-904; page-1 `SearchPage` constructions at ~925, ~952, ~1010)
- Modify: `src/localmail/api/search.py` (`run_search` ~155-194)
- Modify: `tests/test_searcher_smart.py`
- Modify: `tests/test_api_search.py`

- [ ] **Step 1: Update the Searcher smart tests (failing)**

In `tests/test_searcher_smart.py`, the `RaisingRewriter` (raises `httpx.ConnectError`) test currently asserts `page.rewrite_skipped is True`. Replace that assertion and add a 404 case.

Change `test_smart_enriches_parsed_and_times_rewrite` assertion:

```python
    assert page.rewrite_status == "applied"
    assert page.rewrite_note is None
```
(replacing `assert page.rewrite_skipped is False`)

Change `test_smart_falls_through_on_rewriter_failure` assertion:

```python
    assert page.rewrite_status == "failed"
    assert page.rewrite_note == "could not reach the rewriter service"
```
(replacing `assert page.rewrite_skipped is True`)

Add a new fake + test after `RaisingRewriter` (which is defined near line 37). First add the 404 fake near the other fakes:

```python
class Status404Rewriter:
    name = "missing-model"
    model = "granite4.1:3b-q8_0"

    def rewrite(self, free_text):
        request = httpx.Request("POST", "http://localhost:11434/api/generate")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)
```

Then add the test (mirror the structure of `test_smart_falls_through_on_rewriter_failure` — use the same `_make_searcher`/fixture pattern that test uses; substitute `Status404Rewriter()` for the rewriter and pass `cfg.search.rewriter_model = "granite4.1:3b-q8_0"` if that test configures the model, otherwise rely on the fake's `.model`):

```python
def test_smart_failed_404_yields_model_pull_note(db_dsn, db_conn):
    s = _make_searcher_with_rewriter(db_dsn, Status404Rewriter())
    try:
        page = s.search("test", smart=True, use_cache=False)
    finally:
        s._pool.close()
    assert page.rewrite_status == "failed"
    assert "granite4.1:3b-q8_0" in page.rewrite_note
    assert "ollama pull granite4.1:3b-q8_0" in page.rewrite_note
```

> NOTE for the implementer: open `tests/test_searcher_smart.py` and reuse its **existing** searcher-construction helper (the one `test_smart_falls_through_on_rewriter_failure` uses — it may be inline rather than a `_make_searcher_with_rewriter` function). Match that test's exact setup/teardown; the model name the classifier interpolates comes from `cfg.search.rewriter_model`, so ensure the searcher's config carries `rewriter_model="granite4.1:3b-q8_0"` (set it the same way the surrounding tests set rewriter config).

- [ ] **Step 2: Update the run_search smart fakes + tests (failing)**

In `tests/test_api_search.py`, rewrite `_fake_searcher_for_smart` to set the new page fields and accept a status/note instead of the bool:

```python
def _fake_searcher_for_smart(
    *, smart_available: bool, page_status: str = "not_requested",
    page_note=None,
):
    s = MagicMock()
    s.smart_available = smart_available
    page = MagicMock()
    page.results = []
    page.search_token = "tok-1"
    page.timing_ms = {"total": 1.0}
    page.has_more_in_pool = False
    page.can_grow_pool = False
    page.candidates_per_arm = 50
    page.page = 1
    page.next_keyset = None
    page.rewrite_status = page_status
    page.rewrite_note = page_note
    s.search.return_value = page
    return s
```

Update the existing callers and assertions:

```python
def test_run_search_forwards_smart_when_available():
    s = _fake_searcher_for_smart(smart_available=True, page_status="applied")
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9, smart=True)
    assert s.search.call_args.kwargs["smart"] is True
    assert out["rewrite_status"] == "applied"
    assert out["rewrite_note"] is None
    assert out["rewrite_skipped"] is False


def test_run_search_smart_surfaces_page_failure():
    s = _fake_searcher_for_smart(
        smart_available=True, page_status="failed",
        page_note="could not reach the rewriter service",
    )
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9, smart=True)
    assert out["rewrite_status"] == "failed"
    assert out["rewrite_note"] == "could not reach the rewriter service"
    assert out["rewrite_skipped"] is True


def test_run_search_smart_without_rewriter_degrades_gracefully():
    s = _fake_searcher_for_smart(smart_available=False)
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9, smart=True)
    assert s.search.call_args.kwargs["smart"] is False
    assert out["rewrite_status"] == "unavailable"
    assert out["rewrite_note"] == "smart search is not configured on this server"
    assert out["rewrite_skipped"] is True
    assert "results" in out


def test_run_search_default_smart_is_false():
    s = _fake_searcher_for_smart(smart_available=True, page_status="not_requested")
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9)
    assert s.search.call_args.kwargs["smart"] is False
    assert out["rewrite_status"] == "not_requested"
    assert out["rewrite_skipped"] is False
```

Replace the empty-ACL test's expected dict:

```python
def test_run_search_empty_acl_short_circuit_includes_rewrite_status():
    """The ACL short-circuit (no grants) keeps the stable wire shape."""
    s = MagicMock()
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[], user_id=9, smart=True)
    assert out == {"results": [], "next_cursor": None, "total_estimate": None,
                   "took_ms": 0.0, "rewrite_skipped": False,
                   "rewrite_status": "not_requested", "rewrite_note": None}
    s.search.assert_not_called()
```

Replace the continuation test to assert `not_attempted`:

```python
def test_run_search_smart_on_continuation_cursor_reports_not_attempted():
    """smart is a page-1 signal: a pool-cursor continuation must NOT re-rewrite
    and reports not_attempted (rewrite_skipped stays False) even when the
    caller re-sends smart=True."""
    from localmail.api.search_cursor import SearchCursor, encode_search_cursor

    s = _fake_searcher_for_smart(smart_available=True, page_status="applied")
    s.continue_page.return_value = s.search.return_value
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=2))
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9, smart=True, cursor=cursor)
    s.search.assert_not_called()
    s.continue_page.assert_called_once()
    assert out["rewrite_status"] == "not_attempted"
    assert out["rewrite_note"] == (
        "smart query rewriting applies to the first page only; "
        "this is a continuation page"
    )
    assert out["rewrite_skipped"] is False
```

- [ ] **Step 3: Run the updated tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_api_search.py tests/test_searcher_smart.py -q`
Expected: FAIL — `SearchPage` has no `rewrite_status`/`rewrite_note`; `run_search` output lacks the new keys.

- [ ] **Step 4: Rename the SearchPage field**

In `src/localmail/search/searcher.py`, replace the `rewrite_skipped` dataclass field (~line 252):

```python
    next_keyset: KeysetCursor | None = None
    rewrite_status: str = NOT_REQUESTED
    rewrite_note: str | None = None
```

Add the import near the top (with the other `localmail.search` imports):

```python
from localmail.search.rewrite_status import (
    APPLIED,
    FAILED,
    NOT_REQUESTED,
    classify_rewrite_failure,
)
```

- [ ] **Step 5: Compute the page-1 outcome in `Searcher.search`**

Replace the rewrite block (currently ~891-904):

```python
        rewrite_status = NOT_REQUESTED
        rewrite_note: str | None = None
        if smart and parsed.free_text.strip():
            t = time.monotonic()
            try:
                assert self._rewriter is not None
                result = self._rewriter.rewrite(parsed.free_text)
                parsed = apply_rewrite(
                    parsed, result,
                    max_expansion_terms=cfg.rewriter_max_expansion_terms,
                )
                rewrite_status = APPLIED
            except (httpx.HTTPError, RewriteParseError) as exc:
                rewrite_status = FAILED
                rewrite_note = classify_rewrite_failure(
                    exc, model=cfg.rewriter_model
                )
                log.warning("smart rewrite skipped: %s", exc)
            timing["rewrite"] = (time.monotonic() - t) * 1000
```

> The `cfg` here is the `SearchConfig` already in scope (the same object whose `rewriter_max_expansion_terms` is read one line up). `rewriter_model` is a field on it.

- [ ] **Step 6: Pass the outcome to all three page-1 SearchPage constructions**

In each of the three page-1 returns (lexical-date ~925, empty-query ~952, hybrid ~1010), replace:

```python
                rewrite_skipped=rewrite_skipped,
```
with:
```python
                rewrite_status=rewrite_status,
                rewrite_note=rewrite_note,
```

(Three occurrences. The `continue_page`/`grow_pool` constructions at ~758/~844 do not set these and correctly keep the defaults.)

- [ ] **Step 7: Update `run_search` status logic**

In `src/localmail/api/search.py`, add the import:

```python
from localmail.search.rewrite_status import (
    NOT_ATTEMPTED,
    NOT_REQUESTED,
    UNAVAILABLE,
    NOTE_NOT_ATTEMPTED,
    NOTE_UNAVAILABLE,
    rewrite_skipped_for_status,
)
```

Replace the ACL short-circuit return (~155-158):

```python
    scoped_filters = _scope_filters_by_acl(filters, allowed_account_ids)
    if scoped_filters is None:
        # total_estimate is "estimate not computed" — uniformly None across
        # every branch (#175). No rewrite was performed, so the empty-ACL
        # short-circuit reports not_requested (#176).
        return {"results": [], "next_cursor": None, "total_estimate": None,
                "took_ms": 0.0, "rewrite_skipped": False,
                "rewrite_status": NOT_REQUESTED, "rewrite_note": None}
```

Replace the body after the branch dispatch (the `next_cursor`/`rewrite_skipped`/return block, ~186-194). Keep the existing `cursor is None` / keyset / else dispatch that builds `page`, then:

```python
    next_cursor = _next_cursor(page, cfg=cfg)
    if cursor is None:
        if smart and not searcher.smart_available:
            status, note = UNAVAILABLE, NOTE_UNAVAILABLE
        else:
            status = page.rewrite_status
            note = page.rewrite_note
    else:
        if smart:
            status, note = NOT_ATTEMPTED, NOTE_NOT_ATTEMPTED
        else:
            status, note = NOT_REQUESTED, None
    return {
        "results": [_to_api_result(r) for r in page.results],
        "next_cursor": next_cursor,
        "total_estimate": None,
        "took_ms": page.timing_ms.get("total", 0.0),
        "rewrite_skipped": rewrite_skipped_for_status(status),
        "rewrite_status": status,
        "rewrite_note": note,
    }
```

Delete the now-unused `effective_smart` / `rewrite_unavailable` lines (~165-166) **only if** they are no longer referenced — `effective_smart` is still passed into `searcher.search(...)` on the `cursor is None` branch, so KEEP `effective_smart`; only `rewrite_unavailable` becomes dead. Remove the `rewrite_unavailable` assignment and the old `rewrite_skipped = rewrite_unavailable or ...` line.

- [ ] **Step 8: Run the affected tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_api_search.py tests/test_searcher_smart.py -q`
Expected: PASS.

- [ ] **Step 9: Run the broader search/serve/mcp suite for regressions**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_serve_search_route.py tests/test_mcp_tools.py tests/test_search_public_api.py -q`
Expected: PASS, except possibly `tests/test_mcp_tools.py::test_tool_search_empty_grants_returns_empty` whose expected dict is updated in Task 3. If that one fails on the missing keys, that is expected — proceed to Task 3.

- [ ] **Step 10: Commit**

```bash
git add src/localmail/search/searcher.py src/localmail/api/search.py \
        tests/test_searcher_smart.py tests/test_api_search.py
git commit -m "feat(search): structured rewrite_status/rewrite_note on search responses (#176)"
```

---

## Task 3: MCP tool expected-dict + docstring

**Files:**
- Modify: `tests/test_mcp_tools.py` (~106-107)
- Modify: `src/localmail/mcp/tools.py` (docstring ~36-40)

- [ ] **Step 1: Update the empty-grants expected dict (failing)**

In `tests/test_mcp_tools.py`, replace the `assert page == {...}` in `test_tool_search_empty_grants_returns_empty`:

```python
    assert page == {"results": [], "next_cursor": None, "total_estimate": None,
                    "took_ms": 0.0, "rewrite_skipped": False,
                    "rewrite_status": "not_requested", "rewrite_note": None}
```

- [ ] **Step 2: Run to verify it fails (if not already passing from Task 2)**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_tools.py::test_tool_search_empty_grants_returns_empty -q`
Expected: PASS now that `run_search` emits the keys (the assertion was the only change). If it was already failing after Task 2 Step 9, it now passes.

- [ ] **Step 3: Update the tool docstring**

In `src/localmail/mcp/tools.py`, replace the `smart` paragraph of `tool_search`'s docstring:

```python
    """Hybrid search, ACL-scoped. Page forward by passing back `next_cursor`.

    `smart` opts into an LLM query rewrite (page 1 only). The response carries
    `rewrite_status` (one of `applied`, `unavailable`, `failed`,
    `not_attempted`, `not_requested`) and an optional curated `rewrite_note`;
    `rewrite_skipped` (kept for back-compat) is True only for `unavailable`
    and `failed`. On a continuation page, `smart` is ignored and the status is
    `not_attempted`.
    """
```

- [ ] **Step 4: Run the MCP tool tests**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_tools.py src/localmail/mcp/tools.py
git commit -m "feat(mcp): rewrite_status/rewrite_note on the search tool (#176)"
```

---

## Task 4: CLI prints the curated note

**Files:**
- Modify: `src/localmail/cli.py` (~676-678)
- Modify: `tests/test_cli_search.py`

- [ ] **Step 1: Write/adjust the failing CLI test**

In `tests/test_cli_search.py`, find the test asserting the `--smart` skipped note (search for `rewrite skipped` / `rewriter unavailable`). Adjust it (or add a new test) so that when the page reports a `failed` status with a note, the CLI prints that note. If the test builds a fake page, set `page.rewrite_status = "failed"` and `page.rewrite_note = "could not reach the rewriter service"` and assert that string appears in stderr. Concretely, add:

```python
def test_search_smart_prints_curated_note(monkeypatch):
    # Build the same fake-searcher harness the other CLI search tests use,
    # returning a page with a failed rewrite outcome.
    page = _fake_cli_page(
        rewrite_status="failed",
        rewrite_note="could not reach the rewriter service",
    )
    # ... wire the fake searcher into the CLI invocation as the sibling tests do ...
    result = runner.invoke(main, ["search", "--smart", "hello"])
    assert "could not reach the rewriter service" in result.output
```

> NOTE: reuse the exact fake/monkeypatch pattern already present in `tests/test_cli_search.py` (the existing smart-note test shows how the CLI's searcher is faked and how a `SearchPage`-like object is produced — mirror it, just set the two new attributes). If the existing fake constructs a real `SearchPage`, pass `rewrite_status=`/`rewrite_note=` to it.

- [ ] **Step 2: Run to verify it fails**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_search.py -k smart -q`
Expected: FAIL — the CLI still prints only the generic "rewriter unavailable" line.

- [ ] **Step 3: Update the CLI note path**

In `src/localmail/cli.py`, replace (~676-678):

```python
    if page.rewrite_status in ("unavailable", "failed", "not_attempted"):
        detail = page.rewrite_note or "ran the original query"
        click.echo(f"note: --smart {detail}", err=True)
```

> Rationale: `page` here is the `SearchPage` from `searcher.search` (the CLI page-1 path), so its status is one of `applied`/`failed`/`not_requested`. `unavailable`/`not_attempted` won't occur on this object but are harmless to include. Only emit the note when there is something to say (i.e. not `applied`/`not_requested`).

- [ ] **Step 4: Run to verify it passes**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_cli_search.py -k smart -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localmail/cli.py tests/test_cli_search.py
git commit -m "feat(cli): --smart prints the curated rewrite note (#176)"
```

---

## Task 5: Docs + GUI type

**Files:**
- Modify: `docs/mcp-usage.md`
- Modify: `gui/src/lib/api/search.ts` (~60-65)
- Modify: `CLAUDE.md` (search subsystem `--smart` paragraph)
- Modify: `README.md` (if it documents the search response shape)

- [ ] **Step 1: GUI type**

In `gui/src/lib/api/search.ts`, extend `SearchResponse` (fields optional so the existing `.test.ts` object-literal fixtures don't need updating — the GUI does not consume these):

```typescript
export interface SearchResponse {
  results: SearchResultRow[];
  next_cursor: string | null;
  total_estimate: number | null;
  took_ms: number;
  rewrite_status?: string;
  rewrite_note?: string | null;
}
```

- [ ] **Step 2: GUI typecheck (no behaviour change)**

Run: `cd gui && npm run check` (or the project's svelte-check script; if none, `npx tsc --noEmit`).
Expected: PASS, no new errors. (If the build script name differs, use whatever `gui/package.json` defines for type checking.)

- [ ] **Step 3: docs/mcp-usage.md**

Add a sentence to the `search` tool section describing `rewrite_status` (the 5 values) and `rewrite_note`, and that `rewrite_skipped` is the derived back-compat bool. Mirror the docstring wording from Task 3 Step 3.

- [ ] **Step 4: CLAUDE.md**

In the `--smart over the wire (HTTP + MCP)` paragraph of the search subsystem section, append a sentence: every search response now also carries `rewrite_status` (5-value enum: applied/unavailable/failed/not_attempted/not_requested) and a curated `rewrite_note`; `rewrite_skipped` is derived (`status ∈ {unavailable, failed}`). Note `not_attempted` is the continuation-page status (closes #176).

- [ ] **Step 5: README.md (only if it documents the response shape)**

Grep `README.md` for `rewrite_skipped`. If present, add the two new fields alongside; if absent, no change.

- [ ] **Step 6: Commit**

```bash
git add docs/mcp-usage.md gui/src/lib/api/search.ts CLAUDE.md README.md
git commit -m "docs: rewrite_status/rewrite_note across MCP usage, CLAUDE.md, GUI type (#176)"
```

---

## Task 6: Full verification gate

- [ ] **Step 1: Full test suite (with MCP extra)**

Run:
```bash
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ \
  --deselect tests/test_daemon_control_socket.py
```
Expected: all pass (the new tests add ~10; total advances past the prior 1508).

- [ ] **Step 2: MCP integration test**

Run:
```bash
unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_integration.py -m integration -v
```
Expected: PASS (skips if the `mcp` client isn't installed).

- [ ] **Step 3: mypy**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean (now 106 files — the new module).

- [ ] **Step 4: No further commit** — code/doc commits already landed per task.

---

## Self-Review notes (for the executor)

- **Spec coverage:** Task 1 = pure module + classifier + derived bool; Task 2 = SearchPage/Searcher/run_search (all 5 statuses, the continuation `not_attempted` fix, ACL short-circuit); Task 3 = MCP shape/docstring; Task 4 = CLI note; Task 5 = docs + GUI type. Every spec section maps to a task.
- **Type consistency:** status constants (`APPLIED`/`UNAVAILABLE`/`FAILED`/`NOT_ATTEMPTED`/`NOT_REQUESTED`) and helpers (`classify_rewrite_failure`, `rewrite_skipped_for_status`, `note_model_unavailable`, `NOTE_*`) are defined in Task 1 and used verbatim in Tasks 2–4.
- **Green-per-commit:** the SearchPage field rename forces Searcher + run_search + their fakes to move together — bundled in Task 2 on purpose. Task 3's only code change (docstring) is independent; its test change is the expected-dict that depends on Task 2.
- **No magic numbers:** the 404 check uses `http.HTTPStatus.NOT_FOUND`.
