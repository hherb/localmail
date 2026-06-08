# `--smart` over MCP + HTTP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the Phase-4 query rewriter (`--smart`) through the HTTP `POST /v1/search` endpoint and the MCP `search` tool, surfacing a `rewrite_skipped` flag on the wire, with graceful degradation when no rewriter is configured.

**Architecture:** Add a public `Searcher.smart_available` property so the shared `api/search.py::run_search` can compute `effective_smart = smart and searcher.smart_available` (no exception-as-control-flow). `run_search` gains a `smart` param applied only on the page-1 branch and emits a stable `rewrite_skipped: bool` on every response. The HTTP route and both MCP layers (tool body + FastMCP tool) thread a `smart` flag through to `run_search`.

**Tech Stack:** Python 3.12, FastAPI, FastMCP, pytest, psycopg, pgvector. No migration, no new config.

**Spec:** [docs/superpowers/specs/2026-06-08-smart-over-mcp-http-design.md](../specs/2026-06-08-smart-over-mcp-http-design.md)

---

## Pre-flight

- [ ] **Step 0: Create the feature branch** (we are on `main`; branch first)

```bash
cd /Users/hherb/src/localmail
git checkout -b smart-over-mcp-http
git add docs/superpowers/specs/2026-06-08-smart-over-mcp-http-design.md \
        docs/superpowers/plans/2026-06-08-smart-over-mcp-http.md
git commit -m "docs: spec + plan for --smart over MCP/HTTP"
```

Run all test commands with `unset VIRTUAL_ENV && uv run …` (the shell's stray
`VIRTUAL_ENV` makes `uv run` pick the wrong interpreter — see CLAUDE.md).

---

## Task 1: `Searcher.smart_available` property

**Files:**
- Modify: `src/localmail/search/searcher.py` (add property after the `config` property, ~line 304)
- Test: `tests/test_searcher_smart.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_searcher_smart.py` (it already imports `Searcher`,
`SearchConfig`, `open_pool`, and defines `_E`, `_R`, `FakeRewriter`,
`_smart_result`):

```python
def test_smart_available_true_when_rewriter_configured(db_dsn):
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=_R(),
                     rewriter=FakeRewriter(_smart_result()))
        assert s.smart_available is True
    finally:
        pool.close()


def test_smart_available_false_when_no_rewriter(db_dsn):
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=_R(),
                     rewriter=None)
        assert s.smart_available is False
    finally:
        pool.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_searcher_smart.py -k smart_available -v`
Expected: FAIL — `AttributeError: 'Searcher' object has no attribute 'smart_available'`.

- [ ] **Step 3: Add the property**

In `src/localmail/search/searcher.py`, immediately after the `config`
property's `return self._cfg` line (the property block ending ~line 304), add:

```python
    @property
    def smart_available(self) -> bool:
        """True when a query rewriter is wired, so ``search(smart=True)`` will
        run instead of raising. The public boundary the api/ layer uses to
        decide whether a requested smart rewrite is possible — never reach into
        ``searcher._rewriter`` (see #71)."""
        return self._rewriter is not None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_searcher_smart.py -k smart_available -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/search/searcher.py tests/test_searcher_smart.py
git commit -m "feat(search): Searcher.smart_available public property"
```

---

## Task 2: `run_search` gains `smart` + emits `rewrite_skipped`

**Files:**
- Modify: `src/localmail/api/search.py` (`run_search`, lines 122-177)
- Test: `tests/test_api_search.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_search.py` (it already imports `run_search` and
`MagicMock`). These build a fake searcher/page like the existing
`test_run_search_calls_searcher_and_maps_results`:

```python
def _fake_searcher_for_smart(*, smart_available: bool, page_rewrite_skipped: bool):
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
    page.rewrite_skipped = page_rewrite_skipped
    s.search.return_value = page
    return s


def test_run_search_forwards_smart_when_available():
    s = _fake_searcher_for_smart(smart_available=True, page_rewrite_skipped=False)
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9, smart=True)
    assert s.search.call_args.kwargs["smart"] is True
    assert out["rewrite_skipped"] is False


def test_run_search_smart_surfaces_page_rewrite_skipped():
    s = _fake_searcher_for_smart(smart_available=True, page_rewrite_skipped=True)
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9, smart=True)
    assert out["rewrite_skipped"] is True


def test_run_search_smart_without_rewriter_degrades_gracefully():
    """smart=True on a server with no rewriter: do NOT raise; run un-rewritten,
    report rewrite_skipped=True, and still return the results dict."""
    s = _fake_searcher_for_smart(smart_available=False, page_rewrite_skipped=False)
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9, smart=True)
    # effective_smart must be False so the searcher's RuntimeError guard never fires
    assert s.search.call_args.kwargs["smart"] is False
    assert out["rewrite_skipped"] is True
    assert "results" in out


def test_run_search_default_smart_is_false():
    s = _fake_searcher_for_smart(smart_available=True, page_rewrite_skipped=False)
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[1], user_id=9)
    assert s.search.call_args.kwargs["smart"] is False
    assert out["rewrite_skipped"] is False


def test_run_search_empty_acl_short_circuit_includes_rewrite_skipped():
    """The ACL short-circuit (no grants) keeps the stable wire shape."""
    s = MagicMock()
    out = run_search(searcher=s, free_text="q", filters={}, limit=20,
                     allowed_account_ids=[], user_id=9, smart=True)
    assert out == {"results": [], "next_cursor": None,
                   "total_estimate": 0, "took_ms": 0.0, "rewrite_skipped": False}
    s.search.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_search.py -k "smart or short_circuit" -v`
Expected: FAIL — `run_search()` got an unexpected keyword argument `smart`
(and the short-circuit dict lacks `rewrite_skipped`).

- [ ] **Step 3: Update `run_search`**

In `src/localmail/api/search.py`, change the `run_search` signature to add
`smart` (keep all existing params; add after `cursor`):

```python
def run_search(
    *,
    searcher: Searcher,
    free_text: str,
    filters: dict[str, Any],
    limit: int,
    allowed_account_ids: list[int],
    user_id: int,
    sort: Literal["rank", "date"] = "rank",
    cursor: str | None = None,
    smart: bool = False,
) -> dict[str, Any]:
```

Update the ACL short-circuit return (currently line ~150-151) to keep the
stable wire shape:

```python
    scoped_filters = _scope_filters_by_acl(filters, allowed_account_ids)
    if scoped_filters is None:
        return {"results": [], "next_cursor": None, "total_estimate": 0,
                "took_ms": 0.0, "rewrite_skipped": False}
```

Replace the body from `cfg = searcher.config` down through the final `return`
with the smart-aware version (the only change to the page-1 branch is
`smart=effective_smart`; the keyset + continuation branches are untouched
because smart is a page-1 signal):

```python
    cfg = searcher.config
    # smart is a page-1 signal: continuation (cursor present) reuses the
    # cached enriched parse and never re-rewrites. effective_smart guards the
    # Searcher's "no rewriter configured" RuntimeError — when smart is asked
    # for but unavailable, degrade gracefully and report rewrite_skipped.
    effective_smart = smart and searcher.smart_available
    rewrite_unavailable = cursor is None and smart and not searcher.smart_available

    if cursor is None:
        query = build_query_string(free_text=free_text, filters=scoped_filters)
        page = searcher.search(query, page_size=limit, user_id=user_id,
                               sort=sort, smart=effective_smart)
    elif is_keyset_cursor(cursor):
        keyset = decode_keyset_cursor(cursor)
        query = build_query_string(free_text=free_text, filters=scoped_filters)
        page = searcher.search(query, page_size=limit, user_id=user_id,
                               sort=sort, keyset_cursor=keyset)
    else:
        parsed = decode_search_cursor(cursor)
        page = _continue_or_grow(searcher, parsed, user_id=user_id, cfg=cfg)

    next_cursor = _next_cursor(page, cfg=cfg)
    rewrite_skipped = rewrite_unavailable or bool(getattr(page, "rewrite_skipped", False))
    return {
        "results": [_to_api_result(r) for r in page.results],
        "next_cursor": next_cursor,
        "total_estimate": None,
        "took_ms": page.timing_ms.get("total", 0.0),
        "rewrite_skipped": rewrite_skipped,
    }
```

Also extend the `run_search` docstring with one line:

```
    ``smart`` requests an LLM query rewrite on page 1 (cursor is None) when the
    searcher has a rewriter configured; the response ``rewrite_skipped`` is
    True when a requested rewrite did not happen (rewriter unavailable, or the
    rewrite call failed) and the un-rewritten query ran instead.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_api_search.py -v`
Expected: PASS (all, including the pre-existing
`test_run_search_calls_searcher_and_maps_results`).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/api/search.py tests/test_api_search.py
git commit -m "feat(search): run_search smart param + rewrite_skipped wire field"
```

---

## Task 3: HTTP `POST /v1/search` smart field

**Files:**
- Modify: `src/localmail/serve/routes/search.py` (`SearchRequest` + `search_endpoint`)
- Test: `tests/test_serve_search_route.py`

- [ ] **Step 1: Update the shared fake + write the failing tests**

In `tests/test_serve_search_route.py`, the `_fake_searcher_returning_one_hit`
helper builds a MagicMock page; add `page.rewrite_skipped = False` next to the
existing `page.next_keyset = None` line so a default search reports a real bool
(not a truthy MagicMock that would fail JSON serialization). Then append:

```python
def test_search_smart_param_is_forwarded_to_searcher(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    fake.smart_available = True
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20, "smart": True},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert fake.search.call_args.kwargs["smart"] is True
    assert r.json()["rewrite_skipped"] is False


def test_search_smart_defaults_false_and_response_carries_flag(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    fake.smart_available = True
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert fake.search.call_args.kwargs["smart"] is False
    assert r.json()["rewrite_skipped"] is False


def test_search_smart_without_rewriter_degrades(
    db_dsn: str, api_token: str, db_conn, api_user,
) -> None:
    _seed_acct_and_grant(db_conn, api_user.id)
    fake = _fake_searcher_returning_one_hit()
    fake.smart_available = False
    app = create_app(db_dsn=db_dsn, searcher=fake)
    c = TestClient(app)
    r = c.post(
        "/v1/search",
        json={"query": "hello", "filters": {}, "limit": 20, "smart": True},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert fake.search.call_args.kwargs["smart"] is False
    assert r.json()["rewrite_skipped"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_search_route.py -k smart -v`
Expected: FAIL — `smart` is ignored (`call_args.kwargs["smart"]` KeyError) and
the response has no `rewrite_skipped` key.

- [ ] **Step 3: Add the field + thread it through**

In `src/localmail/serve/routes/search.py`, add to `SearchRequest` (after
`cursor`):

```python
    # Opt-in LLM query rewrite (Phase 4). Ignored gracefully when the server
    # has no rewriter configured — the response's rewrite_skipped reflects it.
    smart: bool = False
```

And pass it in the `run_search(...)` call inside `search_endpoint`, after
`cursor=req.cursor,`:

```python
        smart=req.smart,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_serve_search_route.py -v`
Expected: PASS (all, including the pre-existing route tests).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/serve/routes/search.py tests/test_serve_search_route.py
git commit -m "feat(serve): POST /v1/search smart field + rewrite_skipped"
```

---

## Task 4: MCP `tool_search` smart param

**Files:**
- Modify: `src/localmail/mcp/tools.py` (`tool_search`, lines 24-45)
- Test: `tests/test_mcp_tools.py`

- [ ] **Step 1: Write the failing tests + update the exact-dict assertion**

In `tests/test_mcp_tools.py`, the existing
`test_tool_search_empty_grants_returns_empty` asserts an exact dict; update its
expected value to include the new stable field:

```python
    assert page == {"results": [], "next_cursor": None,
                    "total_estimate": 0, "took_ms": 0.0, "rewrite_skipped": False}
```

Then append a graceful-degradation test (the `_lexical_searcher` helper builds a
`Searcher(..., rewriter=None)`, so `smart_available` is False):

```python
def test_tool_search_smart_without_rewriter_degrades(db_dsn, db_conn):
    uid = create_user(db_conn, "smartless", "hunter2")
    acct = _insert_account(db_conn, "smartless-acct")
    grant_account(db_conn, uid, acct)
    _insert_message(db_conn, acct, "invoice", "the invoice body")
    db_conn.commit()
    acl = allowed_account_ids(db_conn, uid)
    searcher = _lexical_searcher(db_dsn)
    try:
        page = tools.tool_search(
            searcher=searcher, user_id=uid, allowed_account_ids=acl,
            query="invoice", sort="date", limit=20, cursor=None, filters={},
            smart=True,
        )
    finally:
        searcher._pool.close()
    assert page["rewrite_skipped"] is True
    assert page["results"]  # search still ran on the un-rewritten query
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_tools.py -k "smart or empty_grants" -v`
Expected: FAIL — `tool_search()` got an unexpected keyword argument `smart`.

- [ ] **Step 3: Add the param**

In `src/localmail/mcp/tools.py`, update `tool_search` to add `smart` (after
`filters`) and forward it:

```python
def tool_search(
    *,
    searcher: Searcher,
    user_id: int,
    allowed_account_ids: list[int],
    query: str,
    sort: Literal["rank", "date"] = "rank",
    limit: int = 50,
    cursor: str | None = None,
    filters: dict[str, Any] | None = None,
    smart: bool = False,
) -> dict[str, Any]:
    """Hybrid search, ACL-scoped. Page forward by passing back `next_cursor`.

    `smart` opts into an LLM query rewrite (page 1 only); the response's
    `rewrite_skipped` is True when the rewrite did not happen and the
    un-rewritten query ran instead.
    """
    return run_search(
        searcher=searcher,
        free_text=query,
        filters=filters or {},
        limit=limit,
        allowed_account_ids=allowed_account_ids,
        user_id=user_id,
        sort=sort,
        cursor=cursor,
        smart=smart,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_tools.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/tools.py tests/test_mcp_tools.py
git commit -m "feat(mcp): tool_search smart param"
```

---

## Task 5: MCP FastMCP `search` tool smart param + description

**Files:**
- Modify: `src/localmail/mcp/server.py` (`search` tool, lines 100-188)
- Test: `tests/test_mcp_tool_descriptions.py` (has the `tools_by_name` fixture +
  `_params` helper, and `test_every_parameter_is_documented` already gates that
  every param carries a description)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_tool_descriptions.py` (it defines the `tools_by_name`
fixture and `_params(tool)` → `tool.inputSchema["properties"]`). Add an explicit
presence test for the new `smart` param:

```python
def test_search_tool_exposes_documented_smart_param(tools_by_name):
    params = _params(tools_by_name["search"])
    assert "smart" in params, "search tool must expose a smart param"
    assert (params["smart"].get("description") or "").strip(), \
        "search.smart must be documented"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_tool_descriptions.py -v`
Expected: FAIL — `test_search_tool_exposes_documented_smart_param` (no `smart`
param) and `test_every_parameter_is_documented` once the param is added without
a description (it won't be — the impl adds the description in the same step).

- [ ] **Step 3: Add the Annotated param + forward it**

In `src/localmail/mcp/server.py`, add to the `search` tool signature (after the
`lang` param, before the closing `)` and `-> dict[str, Any]:`):

```python
        smart: Annotated[bool, Field(description=(
            "Opt into an LLM query rewrite of the free-text query before "
            "searching (page 1 only): a richer vector query, synonym expansion "
            "OR-ed into the keyword arms, and natural-language filters. The "
            "response field `rewrite_skipped` is true when the rewrite did not "
            "happen (no rewriter configured, or the rewrite call failed) and "
            "the original query ran instead. Defaults to false."))] = False,
```

And forward it in the `tools.tool_search(...)` call (after `filters=filters,`):

```python
                smart=smart,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `unset VIRTUAL_ENV && uv run pytest tests/test_mcp_tool_descriptions.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/localmail/mcp/server.py tests/test_mcp_tool_descriptions.py
git commit -m "feat(mcp): search tool smart param with agent-facing docs"
```

---

## Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md` (Phase-4 + MCP sections)
- Modify: `docs/mcp-usage.md` (search tool param table)
- Modify: `README.md` (smart section — note HTTP/MCP availability)

- [ ] **Step 1: Update CLAUDE.md**

In the Phase-4 paragraph, the rewriter is described as wired into the Searcher +
CLI. Add a sentence noting HTTP + MCP exposure. Find the sentence in the
Phase 4 block and append:

```
The rewriter is also exposed on the wire: `POST /v1/search` accepts a `smart`
body field and the MCP `search` tool a `smart` param; both surface
`rewrite_skipped` on the response (always present, default false). The api/
layer gates it via the public `Searcher.smart_available` property — when smart
is requested but no rewriter is configured, the search runs un-rewritten and
`rewrite_skipped` is true (graceful; the CLI still hard-errors, being
interactive).
```

In the MCP server section's `search(...)` tool signature bullet, add `smart` to
the parameter list:

```
  - `search(query, sort="rank"|"date", limit, cursor, account_ids, folder_ids,
    date_from, date_to, from_addr, to, subject, has_attachment, lang, smart)` —
    hybrid search; `smart=true` runs the Phase-4 LLM rewrite (page 1), response
    `rewrite_skipped` reflects whether it happened; page by re-calling with
    `next_cursor`; a cursor-expired error means re-run without a cursor.
```

- [ ] **Step 2: Update docs/mcp-usage.md**

In the tools table (line ~119), add `smart` to the `search` params cell and note
it in the description; the params list becomes:
`query`, `sort="rank"|"date"`, `limit`, `cursor`, `account_ids`, `folder_ids`,
`date_from`, `date_to`, `from_addr`, `to`, `subject`, `has_attachment`, `lang`,
`smart`. Append to that row's description:
" Pass `smart=true` for a local LLM query rewrite; the response's
`rewrite_skipped` is true if the rewrite was unavailable or failed."

- [ ] **Step 3: Update README.md**

At the end of the `### Smart query rewriting (--smart, opt-in)` section (after
line 638), add a short paragraph:

```markdown
The same rewrite is available over the network read surfaces: `POST /v1/search`
accepts a `smart` boolean in the request body, and the MCP `search` tool a
`smart` parameter. Both responses carry a `rewrite_skipped` flag (always
present). Unlike the CLI — which hard-errors when no rewriter is configured —
the wire endpoints degrade gracefully: an un-rewritten search runs and
`rewrite_skipped` is `true`.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/mcp-usage.md README.md
git commit -m "docs: --smart over /v1/search + MCP search tool"
```

---

## Task 7: Full verification

- [ ] **Step 1: Run the full suite**

Run: `unset VIRTUAL_ENV && uv run pytest -q tests/ --deselect tests/test_daemon_control_socket.py`
Expected: all pass (the prior baseline was 1495 passed; this adds ~10 tests, so
expect ~1505 passed, 14 deselected). The macOS `AF_UNIX path too long`
deselect + harmless psycopg_pool teardown ResourceWarnings are pre-existing.

- [ ] **Step 2: Run mypy**

Run: `unset VIRTUAL_ENV && uv run mypy src/localmail`
Expected: `Success: no issues found in 105 source files`.

- [ ] **Step 3: Fix any failures, then re-run Steps 1-2 until green.**

---

## Self-review notes (for the implementer)

- `bool(getattr(page, "rewrite_skipped", False))` is deliberate: real
  `SearchPage` always has the `rewrite_skipped` field (default `False`), but
  MagicMock test pages auto-create truthy attributes — the `bool()` keeps the
  wire value JSON-serializable and the `getattr` default is pure defence.
- `smart` is applied **only** on the `cursor is None` page-1 branch. The keyset
  and pool-continuation branches are intentionally untouched — `rewrite_skipped`
  is a page-1 signal (continuation reuses the cached parse; see CLAUDE.md
  Phase-4 note).
- `rewrite_unavailable` is gated on `cursor is None`, so continuation pages
  report `rewrite_skipped=false` even if a client re-sends `smart=true`.
- The ACL short-circuit and `tool_search` empty-grants exact-dict assertion both
  gain `rewrite_skipped: False` to preserve the stable wire shape (D3).
```
