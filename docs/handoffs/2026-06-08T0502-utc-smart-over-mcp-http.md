# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-08 (`--smart` over MCP + HTTP).** This session shipped
> the §1 "MCP / HTTP `smart=` param" follow-up from the prior handoff: the
> Phase-4 LLM query rewriter is now exposed over `POST /v1/search` and the MCP
> `search` tool, with a stable `rewrite_skipped` wire field and graceful
> degradation. Work is on branch **`smart-over-mcp-http`**, opened as
> **PR #174** — **CI running at handoff; merge once green.** The prior session's
> §0 (merge PR #173, default `rewriter_model` → `granite4.1:3b-q8_0`) **was
> already merged** before this session at `4737673`; `main == origin/main ==
> 4737673`. Suite green locally (**1508 passed, 14 deselected**, run with
> `--extra mcp` incl. integration), mypy clean (105 files).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. Daemon: hot-reload account set, heartbeats, DB command queue,
two-plane supervision, non-blocking lifecycle + admin panel (2B.1–2B.5). Admin
UI: account CRUD (2A.3), user management (2A.4), archive imports (2A.5). Hybrid
search (Phases 1+2) + an HTTPS GUI server + a remote MCP server (Phase 3) +
the opt-in `--smart` LLM query rewriter (Phase 4, now also on the wire) are all
shipped. A Tauri + Svelte GUI lives under `gui/`. See [CLAUDE.md](CLAUDE.md),
[README.md](README.md).

## What we did this session

### A. Confirmed prior §0 already merged (no action carried)

PR #173 (default `rewriter_model` → `granite4.1:3b-q8_0`) merged into `main` at
`4737673` between sessions; the remote branch was deleted. Cleaned up the stale
local `change-default-rewriter-model` branch. **§0 closed.**

### B. `--smart` over MCP + HTTP (branch `smart-over-mcp-http`, PR #174)

Exposes the Phase-4 query rewriter over the two network read surfaces. Design
decisions (D1/D2/D3) in
[docs/superpowers/specs/2026-06-08-smart-over-mcp-http-design.md](docs/superpowers/specs/2026-06-08-smart-over-mcp-http-design.md);
task-by-task plan in
[docs/superpowers/plans/2026-06-08-smart-over-mcp-http.md](docs/superpowers/plans/2026-06-08-smart-over-mcp-http.md).

- **D1 — graceful degradation:** `smart=true` to a server with no rewriter
  configured does **not** hard-fail — the un-rewritten query runs and
  `rewrite_skipped=true`. (The CLI keeps its interactive hard-error.)
- **D2 — public boundary:** new `Searcher.smart_available` property
  (`self._rewriter is not None`); `run_search` computes `effective_smart = smart
  and searcher.smart_available`, so the Searcher's `RuntimeError` guard is never
  triggered and the api/ layer never touches `searcher._rewriter` (#71).
- **D3 — stable wire shape:** every search response carries
  `rewrite_skipped: bool` (default `false`), incl. the ACL short-circuit.
- `smart` applies on the **page-1 branch only** (`cursor is None`);
  continuation/keyset pages report `rewrite_skipped=false`.

Commits on the branch (oldest→newest):
- `2e2cefb` docs: spec + plan
- `93fcf9c` feat(search): `Searcher.smart_available` public property
- `cf9089c` feat(search): `run_search` smart param + `rewrite_skipped` wire field
- `aeb09da` feat(serve): `POST /v1/search` smart field + rewrite_skipped
- `1297d83` feat(mcp): `tool_search` smart param
- `2283a25` feat(mcp): `search` tool smart param with agent-facing docs
- `0e6b76d` docs: --smart over /v1/search + MCP search tool (CLAUDE.md, README, mcp-usage.md)
- `a520642` test: continuation-cursor rewrite_skipped + end-to-end MCP smart=true

**Process:** brainstorm → spec (approved) → plan → subagent-driven TDD (one fresh
implementer subagent per task) → final whole-branch code review. The review
returned **ready to merge** with two test-coverage gaps (no logic bugs), both
closed in `a520642`: an explicit continuation-cursor `rewrite_skipped=false`
unit test, and extending the end-to-end MCP integration test to call
`search(smart=true)` and assert the wire `rewrite_skipped` (the cited
acceptance). **No migration, no new config.**

## What's next

### 0. **Merge PR #174** *(immediate)*
```bash
gh pr checks 174                 # wait for green (Linux CI is the real signal)
gh pr merge 174 --squash         # then: git checkout main && git pull --prune
```
**Acceptance:** CI green on Linux; squash-merge; `main` advances past `4737673`;
delete the local + remote `smart-over-mcp-http` branch.

### 1. **Remaining Phase-4 follow-ups (low priority; non-blocking)**
   - **Rewrite-result caching** — each `--smart` query hits Ollama fresh. Add a
     bounded per-process LRU keyed on free-text if it proves hot.
     **Acceptance:** repeated identical `--smart` query shows near-zero `rewrite`
     timing on the 2nd call; cache bounded.
   - **Actionable rewrite-failure message / pre-flight probe** — when `--smart`
     falls through because the model isn't pulled, the CLI prints a **generic**
     `note: --smart rewrite skipped (rewriter unavailable)`; the actionable
     Ollama 404 detail (`model '…' not found, try pulling it first`) is logged
     but not surfaced. **Acceptance:** a missing-model fall-through tells the user
     which `ollama pull` fixes it. (On the wire, the same opacity applies —
     `rewrite_skipped=true` carries no reason; consider a `rewrite_note` field if
     a consumer needs it.)
   - **Cloud/other rewriter backends** — `rewriter_backend` Literal stays
     `"ollama"`; widen only if a non-local backend is wanted (privacy tradeoff).
   - Each non-trivial item needs its own brainstorm → spec → plan.

### 2. **Remaining MCP follow-ups (carried, low priority)**
   - Full OAuth 2.1 discovery (Approach B); `streamablehttp_client` →
     `streamable_http_client` rename (DeprecationWarning in
     `tests/test_mcp_integration.py`).

### 3. **Upstream-blocked (not actionable)**
   - **#90** (glib via Tauri stack bump; Dependabot alert) and **#25**
     (websockets/uvicorn depwarn) — both upstream-blocked.

## Open decisions & risks
1. **PR #174 is open, not merged** — branch `smart-over-mcp-http` @ `a520642`,
   base `main` @ `4737673`. First action next session is §0 (confirm CI green +
   squash-merge + branch cleanup).
2. **Opaque rewrite-failure signal** *(carried + extended)* — the no-silent-
   failure contract is honoured (search still runs; `rewrite_skipped` is set),
   but neither the CLI note nor the wire flag tells the user *why* the rewrite
   was skipped. Optional polish (see §1 bullet 2).
3. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
   locally on macOS (`AF_UNIX path too long`); pre-existing, env-specific.
   Deselect from the local gate; Linux CI is the real signal. Also: pre-existing
   psycopg_pool teardown `ResourceWarning`s in the suite tail (harmless), and
   `websockets.legacy`/`streamable_http_client` DeprecationWarnings from the MCP
   integration test (tracked: #25 + the rename in §2).
4. **MCP tests need the extra** — run `uv run --extra mcp pytest` to actually
   exercise `test_mcp_*` (they `importorskip("mcp")` otherwise); the integration
   tests are `-m integration`-gated and only run when explicitly selected.
5. **No ROADMAP.md** in this repo *(carried)* — slice status lives in
   NEXT_SESSION/handoffs + the specs. The `/nextsession` ROADMAP step is a no-op.
6. **`.claude/` local files** stay untracked, by design (incl.
   `.claude/scheduled_tasks.lock` — the lone uncommitted entry).

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/*.lock
git branch -vv                           # main (4737673) + smart-over-mcp-http (a520642)
git --no-pager log --oneline -8
gh pr list --state open                  # expect #174 until merged
gh issue list --state open --limit 40    # #90, #25 (both upstream-blocked)

# §0 — merge the --smart-over-wire PR once CI is green:
gh pr checks 174
gh pr merge 174 --squash
git checkout main && git pull --prune
git branch -d smart-over-mcp-http        # local cleanup after squash-merge

# Suite + types (use --extra mcp so the MCP tests actually run):
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ \
  --deselect tests/test_daemon_control_socket.py            # expect 1507+ passed
unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_integration.py -m integration -v
unset VIRTUAL_ENV && uv run mypy src/localmail              # expect clean, 105 files
```

### Try the new wire `smart` (needs a running Ollama + a serve/MCP instance)
```bash
# HTTP: POST /v1/search with a smart body field; response carries rewrite_skipped.
curl -sk -X POST https://localhost:8443/v1/search \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"tax stuff from the accountant last summer","smart":true}' | jq '.rewrite_skipped'
# MCP: call the `search` tool with {"query": "...", "smart": true}; the result's
# rewrite_skipped reflects whether the rewrite happened. With no rewriter
# configured on the server, smart degrades gracefully → rewrite_skipped=true.
```

`main` at `4737673` (== `origin/main`). Branch `smart-over-mcp-http` @ `a520642`
(pushed, PR #174 open). Latest migration `0027_import_jobs_owner.sql`; next free
slot `0028_*.sql`. **No migration this session.**
