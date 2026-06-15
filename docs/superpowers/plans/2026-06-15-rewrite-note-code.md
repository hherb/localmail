# `rewrite_note_code` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a stable, machine-readable `rewrite_note_code` to every search response, present 1:1 with the curated `rewrite_note` and `null` when the note is `null`, so machine clients can switch on the rewrite outcome without string-matching human text.

**Architecture:** The code is the single source of truth; the human note is rendered *from* the code by one pure function (`note_for_code`). `classify_rewrite_failure` returns a code (no longer a note). `SearchPage` and the `/v1/search` + MCP response dicts gain the additive `rewrite_note_code` field. No migration, no new dependency.

**Tech Stack:** Python 3.12, `pytest`, `httpx`, FastAPI. Pure-module-first per repo conventions; TDD throughout.

Spec: [docs/superpowers/specs/2026-06-15-rewrite-note-code-design.md](../specs/2026-06-15-rewrite-note-code-design.md).

Run all test commands with `unset VIRTUAL_ENV && uv run …` to avoid the wrong-interpreter gotcha (see CLAUDE.md).

---

### Task 1: Pure module — codes, code-returning classifier, `note_for_code` renderer

**Files:**
- Modify: `src/localmail/search/rewrite_status.py`
- Test: `tests/test_rewrite_status.py`

- [ ] **Step 1: Rewrite the unit tests to assert on codes + the renderer**

Replace the body of `tests/test_rewrite_status.py` below the `_status_error` helper. The full file content:

```python
"""Unit tests for the pure rewrite-outcome status/note/code helpers."""
import httpx
import pytest

from localmail.search.rewrite_status import (
    APPLIED,
    CONTINUATION_PAGE,
    FAILED,
    MISSING_MODEL,
    NOT_ATTEMPTED,
    NOT_CONFIGURED,
    NOT_REQUESTED,
    UNAVAILABLE,
    UNPARSEABLE,
    UNREACHABLE,
    classify_rewrite_failure,
    note_for_code,
    rewrite_skipped_for_status,
)
from localmail.search.rewriter import RewriteParseError


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://localhost:11434/api/generate")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


def test_classify_404_returns_missing_model_code():
    assert classify_rewrite_failure(_status_error(404)) == MISSING_MODEL


def test_classify_non_404_status_returns_unreachable_code():
    assert classify_rewrite_failure(_status_error(500)) == UNREACHABLE


def test_classify_connect_error_returns_unreachable_code():
    assert classify_rewrite_failure(httpx.ConnectError("down")) == UNREACHABLE


def test_classify_parse_error_returns_unparseable_code():
    assert classify_rewrite_failure(RewriteParseError("bad")) == UNPARSEABLE


def test_note_for_missing_model_interpolates_model():
    note = note_for_code(MISSING_MODEL, model="granite4.1:3b-q8_0")
    assert "granite4.1:3b-q8_0" in note
    assert "ollama pull granite4.1:3b-q8_0" in note


def test_note_for_missing_model_without_model_raises():
    with pytest.raises(ValueError):
        note_for_code(MISSING_MODEL)


@pytest.mark.parametrize(
    "code,expected",
    [
        (UNREACHABLE, "could not reach the rewriter service"),
        (UNPARSEABLE, "the rewriter returned an unparseable response"),
        (NOT_CONFIGURED, "smart search is not configured on this server"),
        (
            CONTINUATION_PAGE,
            "smart query rewriting applies to the first page only; "
            "this is a continuation page",
        ),
    ],
)
def test_note_for_static_codes(code, expected):
    assert note_for_code(code) == expected


def test_note_for_every_code_is_nonempty():
    for code in (
        MISSING_MODEL, UNREACHABLE, UNPARSEABLE, NOT_CONFIGURED, CONTINUATION_PAGE,
    ):
        assert note_for_code(code, model="m")  # rendered, non-empty


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
Expected: FAIL — `ImportError` (cannot import `MISSING_MODEL` / `note_for_code` etc.).

- [ ] **Step 3: Update `rewrite_status.py` to the code-canonical model**

Replace the entire contents of `src/localmail/search/rewrite_status.py` with:

```python
"""Pure helpers describing the per-page outcome of a smart query rewrite.

No IO, no FastAPI — reusable by the api/ layer and any future transport.
The wire carries a ``rewrite_status`` (one of the status constants), a
machine-readable ``rewrite_note_code`` (one of the code constants, or ``None``),
and the optional curated human ``rewrite_note``. The **code is canonical**: each
note is rendered *from* its code by ``note_for_code`` so the two never drift.
Raw exception text never leaves the ``Searcher`` (only these curated strings
travel).
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

RewriteNoteCode = Literal[
    "missing_model", "unreachable", "unparseable",
    "not_configured", "continuation_page",
]

MISSING_MODEL: RewriteNoteCode = "missing_model"
UNREACHABLE: RewriteNoteCode = "unreachable"
UNPARSEABLE: RewriteNoteCode = "unparseable"
NOT_CONFIGURED: RewriteNoteCode = "not_configured"
CONTINUATION_PAGE: RewriteNoteCode = "continuation_page"

NOTE_UNAVAILABLE = "smart search is not configured on this server"
NOTE_NOT_ATTEMPTED = (
    "smart query rewriting applies to the first page only; "
    "this is a continuation page"
)
NOTE_UNREACHABLE = "could not reach the rewriter service"
NOTE_UNPARSEABLE = "the rewriter returned an unparseable response"

_SKIPPED_STATUSES: frozenset[str] = frozenset({UNAVAILABLE, FAILED})

# Static (model-independent) notes keyed by code. ``missing_model`` is absent
# because its note interpolates the configured model name (see note_for_code).
_STATIC_NOTES: dict[RewriteNoteCode, str] = {
    UNREACHABLE: NOTE_UNREACHABLE,
    UNPARSEABLE: NOTE_UNPARSEABLE,
    NOT_CONFIGURED: NOTE_UNAVAILABLE,
    CONTINUATION_PAGE: NOTE_NOT_ATTEMPTED,
}


def note_model_unavailable(model: str) -> str:
    """Actionable note for an Ollama 'model not pulled' (404) failure."""
    return (
        f"rewriter model {model!r} is not available; "
        f"pull it with: ollama pull {model}"
    )


def classify_rewrite_failure(exc: Exception) -> RewriteNoteCode:
    """Map a typed rewriter failure to a stable machine-readable code.

    The code is model-independent; the human note is rendered later via
    ``note_for_code`` (which is where the configured model name is needed).
    """
    if (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == HTTPStatus.NOT_FOUND
    ):
        return MISSING_MODEL
    if isinstance(exc, RewriteParseError):
        return UNPARSEABLE
    return UNREACHABLE


def note_for_code(code: RewriteNoteCode, *, model: str | None = None) -> str:
    """Render the curated human note for a code.

    The code is the single source of truth; the note is derived from it (and,
    for ``missing_model``, the configured model name). Total over the
    ``RewriteNoteCode`` Literal.
    """
    if code == MISSING_MODEL:
        if model is None:
            raise ValueError("missing_model note requires a model name")
        return note_model_unavailable(model)
    return _STATIC_NOTES[code]


def rewrite_skipped_for_status(status: str) -> bool:
    """Back-compat bool: a rewrite was *skipped* only when unavailable/failed."""
    return status in _SKIPPED_STATUSES
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_rewrite_status.py -q`
Expected: PASS (all tests green).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/rewrite_status.py tests/test_rewrite_status.py
git commit -m "feat(search): code-canonical rewrite notes (classify->code + note_for_code)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `SearchPage.rewrite_note_code` + Searcher threading

**Files:**
- Modify: `src/localmail/search/searcher.py` (import block ~30-35; `SearchPage` ~258-259; page-1 block ~898-914; three `SearchPage` constructions at ~942, ~968, ~1029)
- Test: `tests/test_searcher_smart.py:113-132`

- [ ] **Step 1: Extend the searcher smart tests to assert on the code**

In `tests/test_searcher_smart.py`, add a code assertion to the two failure tests and a null-code assertion to the applied test.

After line 98 (`assert page.rewrite_note is None`) in `test_smart_applies_rewrite` (the test ending around line 98), add:

```python
    assert page.rewrite_note_code is None
```

After line 114 (`assert page.rewrite_note == "could not reach the rewriter service"`) add:

```python
    assert page.rewrite_note_code == "unreachable"
```

After line 132 (`assert "ollama pull granite4.1:3b-q8_0" in page.rewrite_note`) add:

```python
    assert page.rewrite_note_code == "missing_model"
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_searcher_smart.py -q -k "applies or failure or 404"`
Expected: FAIL — `AttributeError: 'SearchPage' object has no attribute 'rewrite_note_code'`.

- [ ] **Step 3: Add the import**

In `src/localmail/search/searcher.py`, change the rewrite_status import block (currently lines 30-35) to add `note_for_code`:

```python
from localmail.search.rewrite_status import (
    APPLIED,
    FAILED,
    NOT_REQUESTED,
    classify_rewrite_failure,
    note_for_code,
)
```

- [ ] **Step 4: Add the `SearchPage` field**

In `src/localmail/search/searcher.py`, after the `rewrite_note: str | None = None` line in the `SearchPage` dataclass (line 259), add:

```python
    rewrite_note_code: str | None = None
```

- [ ] **Step 5: Set the code in the page-1 outcome block**

In `Searcher.search`, after the `rewrite_note: str | None = None` init (line 899) add:

```python
        rewrite_note_code: str | None = None
```

Then replace the `except` body (currently lines 910-913):

```python
            except (httpx.HTTPError, RewriteParseError) as exc:
                rewrite_status = FAILED
                rewrite_note = classify_rewrite_failure(exc, model=cfg.rewriter_model)
                log.warning("smart rewrite skipped: %s", exc)
```

with:

```python
            except (httpx.HTTPError, RewriteParseError) as exc:
                rewrite_status = FAILED
                rewrite_note_code = classify_rewrite_failure(exc)
                rewrite_note = note_for_code(
                    rewrite_note_code, model=cfg.rewriter_model
                )
                log.warning("smart rewrite skipped: %s", exc)
```

- [ ] **Step 6: Thread the code into all three `SearchPage` constructions**

There are three `SearchPage(...)` returns that pass `rewrite_status=` / `rewrite_note=` (the lexical-date branch ~942-943, the empty-query fallback ~968-969, and the hybrid path ~1029-1030). In each, add a `rewrite_note_code=rewrite_note_code,` line immediately after the `rewrite_note=rewrite_note,` line. The three edited tails read:

```python
                rewrite_status=rewrite_status,
                rewrite_note=rewrite_note,
                rewrite_note_code=rewrite_note_code,
            )
```

(Apply the same two-line addition at each of the three sites; the indentation matches each existing block.)

- [ ] **Step 7: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_searcher_smart.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_searcher_smart.py
git commit -m "feat(search): carry rewrite_note_code on SearchPage

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `api/search.py` — `rewrite_note_code` on the response dict

**Files:**
- Modify: `src/localmail/api/search.py` (import block 24-31; empty-ACL short-circuit ~168-170; status/note block ~197-219)
- Test: `tests/test_api_search.py` (`_fake_searcher_for_smart` ~212-230 + the smart tests ~233-308)

- [ ] **Step 1: Update the api-search tests**

In `tests/test_api_search.py`, change `_fake_searcher_for_smart` to accept and set a code on the page. Replace its signature + body (lines 212-230) with:

```python
def _fake_searcher_for_smart(
    *, smart_available: bool, page_status: str = "not_requested",
    page_note=None, page_note_code=None,
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
    page.rewrite_note_code = page_note_code
    s.search.return_value = page
    return s
```

Then add code assertions. After line 240 (`assert out["rewrite_skipped"] is False` in `test_run_search_forwards_smart_when_available`) add:

```python
    assert out["rewrite_note_code"] is None
```

In `test_run_search_smart_surfaces_page_failure`, change the fake construction (lines 244-247) to pass the code:

```python
    s = _fake_searcher_for_smart(
        smart_available=True, page_status="failed",
        page_note="could not reach the rewriter service",
        page_note_code="unreachable",
    )
```

and after line 252 (`assert out["rewrite_skipped"] is True`) add:

```python
    assert out["rewrite_note_code"] == "unreachable"
```

In `test_run_search_smart_without_rewriter_degrades_gracefully`, after line 265 (`assert out["rewrite_skipped"] is True`) add:

```python
    assert out["rewrite_note_code"] == "not_configured"
```

In `test_run_search_default_smart_is_false`, after line 275 (`assert out["rewrite_skipped"] is False`) add:

```python
    assert out["rewrite_note_code"] is None
```

In `test_run_search_empty_acl_short_circuit_includes_rewrite_status`, replace the expected dict (lines 283-285) with:

```python
    assert out == {"results": [], "next_cursor": None, "total_estimate": None,
                   "took_ms": 0.0, "rewrite_skipped": False,
                   "rewrite_status": "not_requested", "rewrite_note": None,
                   "rewrite_note_code": None}
```

In `test_run_search_smart_on_continuation_cursor_reports_not_attempted`, after line 308 (`assert out["rewrite_skipped"] is False`) add:

```python
    assert out["rewrite_note_code"] == "continuation_page"
```

- [ ] **Step 2: Run to verify failure**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_search.py -q`
Expected: FAIL — `KeyError: 'rewrite_note_code'` / dict-equality mismatch.

- [ ] **Step 3: Update the imports in `api/search.py`**

Replace the import block (lines 24-31) with:

```python
from localmail.search.rewrite_status import (
    CONTINUATION_PAGE,
    NOT_ATTEMPTED,
    NOT_CONFIGURED,
    NOT_REQUESTED,
    UNAVAILABLE,
    note_for_code,
    rewrite_skipped_for_status,
)
```

(`NOTE_NOT_ATTEMPTED` and `NOTE_UNAVAILABLE` are removed — their notes now come from `note_for_code`.)

- [ ] **Step 4: Add the field to the empty-ACL short-circuit**

In `run_search`, replace the empty-ACL return (lines 168-170):

```python
        return {"results": [], "next_cursor": None, "total_estimate": None,
                "took_ms": 0.0, "rewrite_skipped": False,
                "rewrite_status": NOT_REQUESTED, "rewrite_note": None}
```

with:

```python
        return {"results": [], "next_cursor": None, "total_estimate": None,
                "took_ms": 0.0, "rewrite_skipped": False,
                "rewrite_status": NOT_REQUESTED, "rewrite_note": None,
                "rewrite_note_code": None}
```

- [ ] **Step 5: Compute and emit the code in the status/note block**

Replace the block from `next_cursor = _next_cursor(page, cfg=cfg)` through the end of the `return {...}` (lines 197-219) with:

```python
    next_cursor = _next_cursor(page, cfg=cfg)
    status: str
    note: str | None
    code: str | None
    if cursor is None:
        if smart and not searcher.smart_available:
            status, code = UNAVAILABLE, NOT_CONFIGURED
            note = note_for_code(NOT_CONFIGURED)
        else:
            status = page.rewrite_status
            note = page.rewrite_note
            code = page.rewrite_note_code
    else:
        if smart:
            status, code = NOT_ATTEMPTED, CONTINUATION_PAGE
            note = note_for_code(CONTINUATION_PAGE)
        else:
            status, note, code = NOT_REQUESTED, None, None
    return {
        "results": [_to_api_result(r) for r in page.results],
        "next_cursor": next_cursor,
        "total_estimate": None,
        "took_ms": page.timing_ms.get("total", 0.0),
        "rewrite_skipped": rewrite_skipped_for_status(status),
        "rewrite_status": status,
        "rewrite_note": note,
        "rewrite_note_code": code,
    }
```

- [ ] **Step 6: Run to verify pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_search.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/api/search.py tests/test_api_search.py
git commit -m "feat(search): emit rewrite_note_code on /v1/search responses

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Wire-presence on the serve route + MCP, and docstrings

**Files:**
- Modify: `src/localmail/mcp/tools.py:39-41` (docstring); `src/localmail/mcp/server.py:155-158` (description); `src/localmail/api/search.py` (the `run_search` docstring ~158-161)
- Test: `tests/test_serve_search_route.py:57-59, 335-337, 375-377`; `tests/test_mcp_tools.py:107-108`

- [ ] **Step 1: Update the serve-route and MCP tests**

In `tests/test_serve_search_route.py`, after line 59 (`page.rewrite_note = None`) add:

```python
    page.rewrite_note_code = None
```

After line 336 (`assert body["rewrite_note"] is None`) add:

```python
    assert body["rewrite_note_code"] is None
```

After line 376 (`assert body["rewrite_note"] == "smart search is not configured on this server"`) add:

```python
    assert body["rewrite_note_code"] == "not_configured"
```

In `tests/test_mcp_tools.py`, replace the fake-search return dict (lines 107-108):

```python
                    "took_ms": 0.0, "rewrite_skipped": False,
                    "rewrite_status": "not_requested", "rewrite_note": None}
```

with:

```python
                    "took_ms": 0.0, "rewrite_skipped": False,
                    "rewrite_status": "not_requested", "rewrite_note": None,
                    "rewrite_note_code": None}
```

- [ ] **Step 2: Run to verify the serve test fails (MCP test should still pass — it stubs the dict)**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_search_route.py -q -k "smart or rewrite"`
Expected: FAIL — `KeyError: 'rewrite_note_code'` on the unavailable-branch assertion.

- [ ] **Step 3: Update the `run_search` docstring in `api/search.py`**

In `src/localmail/api/search.py`, replace the `smart` paragraph of the `run_search` docstring (lines 158-161) with:

```python
    ``smart`` requests an LLM query rewrite on page 1 (cursor is None) when the
    searcher has a rewriter configured. The response carries ``rewrite_status``
    (a 5-value enum), an optional curated human ``rewrite_note``, and a stable
    machine-readable ``rewrite_note_code`` (``missing_model`` / ``unreachable``
    / ``unparseable`` / ``not_configured`` / ``continuation_page``, or ``None``
    when there is no note). ``rewrite_skipped`` stays True only when a requested
    rewrite did not happen (rewriter unavailable, or the rewrite call failed).
```

- [ ] **Step 4: Update the MCP `search` tool docstring**

In `src/localmail/mcp/tools.py`, replace lines 39-41:

```python
    `rewrite_status` (one of `applied`, `unavailable`, `failed`,
    `not_attempted`, `not_requested`) and an optional curated `rewrite_note`;
    `rewrite_skipped` (kept for back-compat) is True only for `unavailable`
```

with:

```python
    `rewrite_status` (one of `applied`, `unavailable`, `failed`,
    `not_attempted`, `not_requested`), an optional curated human `rewrite_note`,
    and a machine-readable `rewrite_note_code` (`missing_model`, `unreachable`,
    `unparseable`, `not_configured`, `continuation_page`, or null);
    `rewrite_skipped` (kept for back-compat) is True only for `unavailable`
```

- [ ] **Step 5: Update the MCP server tool description**

In `src/localmail/mcp/server.py`, replace lines 155-158:

```python
            "response carries `rewrite_status` (one of `applied`, "
            "`unavailable`, `failed`, `not_attempted`, `not_requested`) and an "
            "optional curated `rewrite_note` with an actionable detail; "
            "`rewrite_skipped` (kept for back-compat) is true only for "
```

with:

```python
            "response carries `rewrite_status` (one of `applied`, "
            "`unavailable`, `failed`, `not_attempted`, `not_requested`), an "
            "optional curated `rewrite_note` with an actionable detail, and a "
            "machine-readable `rewrite_note_code` (`missing_model`, "
            "`unreachable`, `unparseable`, `not_configured`, "
            "`continuation_page`, or null); "
            "`rewrite_skipped` (kept for back-compat) is true only for "
```

- [ ] **Step 6: Run the touched test files to verify pass**

Run: `unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_serve_search_route.py tests/test_mcp_tools.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/localmail/mcp/tools.py src/localmail/mcp/server.py src/localmail/api/search.py tests/test_serve_search_route.py tests/test_mcp_tools.py
git commit -m "docs(search): document rewrite_note_code on HTTP + MCP search surfaces

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: CLAUDE.md note + full-suite + type check

**Files:**
- Modify: `CLAUDE.md` (the "Structured rewrite outcome (#176, #175)" paragraph)

- [ ] **Step 1: Add a sentence to the CLAUDE.md structured-rewrite paragraph**

In `CLAUDE.md`, find the paragraph beginning `**Structured rewrite outcome (#176, #175):**`. At the end of that paragraph (after the design-doc link sentence), append:

```markdown
Every response also carries a machine-readable **`rewrite_note_code`** (1:1 with
the curated note, `null` when the note is `null`): `missing_model` / `unreachable`
/ `unparseable` (the three `failed` causes), `not_configured` (`unavailable`),
`continuation_page` (`not_attempted`). The **code is canonical** —
`rewrite_status.classify_rewrite_failure(exc)` returns the code (no `model` arg)
and the pure `note_for_code(code, *, model=None)` renders the human note from it,
so the two cannot drift. See
[docs/superpowers/specs/2026-06-15-rewrite-note-code-design.md](docs/superpowers/specs/2026-06-15-rewrite-note-code-design.md).
```

- [ ] **Step 2: Run the full suite (MCP extra; deselect the macOS-only socket flake)**

Run:
```bash
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ \
  --deselect tests/test_daemon_control_socket.py
```
Expected: PASS (all previously-passing tests still green; the new assertions pass).

- [ ] **Step 3: Type-check**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: clean (no new errors). The `_STATIC_NOTES: dict[RewriteNoteCode, str]` and the `note_for_code` Literal branches type-check.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: note rewrite_note_code in CLAUDE.md

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 5: Push + open PR**

```bash
git push -u origin feat/rewrite-note-code
gh pr create --fill
```

---

## Self-review notes

- **Spec coverage:** new wire field (Task 3/4), code↔status↔note table (Tasks 1-3), code-canonical renderer (Task 1), `classify_rewrite_failure` signature change (Task 1, callers updated Task 2), `SearchPage` field (Task 2), docstrings (Task 4), CLAUDE.md (Task 5). All spec sections mapped.
- **Type consistency:** code constants `MISSING_MODEL`/`UNREACHABLE`/`UNPARSEABLE`/`NOT_CONFIGURED`/`CONTINUATION_PAGE` and functions `classify_rewrite_failure(exc)` / `note_for_code(code, *, model=None)` are used identically across all tasks. The field name `rewrite_note_code` is consistent on `SearchPage`, the api dict, and all tests.
- **No placeholders:** every code/test step shows full content.
- **Note on the existing `note_model_unavailable` and `NOTE_*` constants:** kept (re-used by `note_for_code`); no dangling references.
```
