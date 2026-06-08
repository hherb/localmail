# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-08 (structured rewrite outcome).** This session shipped
> the §1 "rewrite_note / structured rewrite outcome" follow-up (#176) **and**
> folded in #175 (uniform `total_estimate`). Work is on branch
> **`rewrite-outcome-status`**, opened as **PR #177** — **CI pending at handoff;
> merge once green.** The prior session's PR #174 (`--smart` over MCP/HTTP) was
> **already merged** before this session at `c91f2a8`; `main == origin/main ==
> c91f2a8`. Suite green locally (**1519 passed, 14 deselected**, `--extra mcp`
> incl. integration), mypy clean (106 files), GUI svelte-check clean.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. Daemon: hot-reload account set, heartbeats, DB command queue,
two-plane supervision, non-blocking lifecycle + admin panel (2B.1–2B.5). Admin
UI: account CRUD (2A.3), user management (2A.4), archive imports (2A.5). Hybrid
search (Phases 1+2) + an HTTPS GUI server + a remote MCP server (Phase 3) +
the opt-in `--smart` LLM query rewriter (Phase 4, on the wire + now with a
structured outcome) are all shipped. A Tauri + Svelte GUI lives under `gui/`.
See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session

### A. Confirmed prior PR #174 already merged (no action carried)

PR #174 (`--smart` over HTTP/MCP) merged into `main` at `c91f2a8` between
sessions; remote branch deleted. Cleaned up the stale local
`smart-over-mcp-http` branch. The prior handoff's §0 is closed.

### B. Structured rewrite outcome (branch `rewrite-outcome-status`, PR #177)

Closes **#176** (continuation-page `smart=true` was a silent no-op; the
`rewrite_skipped: false` it reported looked like "smart applied") and the
carried "opaque rewrite-failure signal" follow-up; also resolves **#175**
(empty-ACL `total_estimate` was `0` while the normal path was `None`).

Design (D1/D2/D3) in
[docs/superpowers/specs/2026-06-08-rewrite-outcome-status-design.md](docs/superpowers/specs/2026-06-08-rewrite-outcome-status-design.md);
task-by-task plan in
[docs/superpowers/plans/2026-06-08-rewrite-outcome-status.md](docs/superpowers/plans/2026-06-08-rewrite-outcome-status.md).

Every search response (`POST /v1/search`, MCP `search`, CLI) now carries two
new flat fields beside the **retained** `rewrite_skipped` bool:

- **`rewrite_status`** (always present): `applied` / `unavailable` / `failed` /
  `not_attempted` / `not_requested`.
- **`rewrite_note`** (`str | null`): curated, actionable detail (e.g.
  `rewriter model 'granite4.1:3b-q8_0' is not available; pull it with: ollama
  pull granite4.1:3b-q8_0`). `null` when nothing to say.
- **`rewrite_skipped`** is **kept but derived** =
  `rewrite_skipped_for_status(status) == status in {unavailable, failed}` —
  so the existing GUI type / MCP docs / wire contract don't break.

Key decisions:
- **`not_attempted`** is the continuation-page status — the #176 fix.
- **Curated notes only** — no raw `str(exc)` on the wire (no host/URL/stack
  leakage to MCP agents); only the configured model name is interpolated.
  Full `str(exc)` still logged at WARNING.
- **Layering:** pure module
  [search/rewrite_status.py](src/localmail/search/rewrite_status.py) holds the
  constants + `classify_rewrite_failure(exc, *, model)` (uses
  `http.HTTPStatus.NOT_FOUND`) + `rewrite_skipped_for_status`. `Searcher.search`
  classifies its own page-1 outcome onto `SearchPage.rewrite_status` /
  `.rewrite_note` (the `rewrite_skipped` **field** is removed from `SearchPage`);
  `api.search.run_search` owns the layer-specific statuses it alone knows
  (`unavailable`, `not_attempted`, the empty-ACL `not_requested`) and derives
  the bool. HTTP route + MCP tool return the `run_search` dict unchanged, so the
  fields propagate with no transport-layer code.

Commits on the branch (oldest→newest):
- `3159219` docs: design spec
- `b609396` docs: implementation plan
- `ddc6847` feat(search): pure rewrite_status module + unit tests
- `1e22b2a` feat(search): structured rewrite_status/rewrite_note (SearchPage/Searcher/run_search) + #175
- `27e0228` feat(mcp): rewrite_status/rewrite_note on the search tool
- `6b6882f` feat(cli): --smart prints the curated rewrite note
- `714e8a9` docs: across MCP usage, CLAUDE.md, README, GUI type
- `4f0873c` fix(search): annotate run_search status/note for mypy

**Process:** brainstorm → spec (approved) → plan → inline TDD (each task tests-
first, green-per-commit) → full-suite + mypy gate. **No migration, no new
config.**

> ⚠️ **Process note for next session:** a mid-session `git reset --hard
> origin/main` discarded the uncommitted #175 edits (they hadn't been committed
> separately). They were re-established as part of the #176 plan (Task 2/3),
> so #175 ships *inside* PR #177 rather than as its own PR. Lesson: commit each
> issue's fix before any `reset --hard`.

## What's next

### 0. **Merge PR #177** *(immediate)*
```bash
gh pr checks 177                 # wait for green (Linux CI is the real signal)
gh pr merge 177 --squash         # then: git checkout main && git pull --prune
git branch -d rewrite-outcome-status
```
**Acceptance:** CI green on Linux; squash-merge; `main` advances past `c91f2a8`;
delete the local + remote `rewrite-outcome-status` branch. Closes #176 + #175.

### 1. **Remaining Phase-4 follow-ups (low priority; non-blocking)**
   - **Rewrite-result caching** — each `--smart` query hits Ollama fresh. Add a
     bounded per-process LRU keyed on free-text if it proves hot.
     **Acceptance:** repeated identical `--smart` query shows near-zero `rewrite`
     timing on the 2nd call; cache bounded.
   - **`rewrite_note` on the *failed-cause* axis is now shipped**, but the
     **continuation** `not_attempted` note and the **unavailable** note are
     fixed strings — fine. If a consumer wants the *cause* enumerated separately
     from the human string (machine-switch on "missing-model" vs "unreachable"),
     add a sub-code; not requested yet.
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
1. **PR #177 is open, not merged** — branch `rewrite-outcome-status` @ `4f0873c`,
   base `main` @ `c91f2a8`. First action next session is §0 (confirm CI green +
   squash-merge + branch cleanup). #176 + #175 both close on merge.
2. **GUI fields are optional in the TS type** (`rewrite_status?`,
   `rewrite_note?`) — they're always present on real responses, but typed
   optional so the existing vitest fixtures needed no change. The GUI does not
   consume them (type-honesty only). Tighten to required if the GUI ever reads
   them.
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
git branch -vv                           # main (c91f2a8) + rewrite-outcome-status (4f0873c)
git --no-pager log --oneline -8
gh pr list --state open                  # expect #177 until merged
gh issue list --state open --limit 40    # #176, #175 (close on #177 merge); #90, #25 upstream-blocked

# §0 — merge the structured-rewrite-outcome PR once CI is green:
gh pr checks 177
gh pr merge 177 --squash
git checkout main && git pull --prune
git branch -d rewrite-outcome-status     # local cleanup after squash-merge

# Suite + types (use --extra mcp so the MCP tests actually run):
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ \
  --deselect tests/test_daemon_control_socket.py            # expect 1519+ passed
unset VIRTUAL_ENV && uv run --extra mcp pytest tests/test_mcp_integration.py -m integration -v
unset VIRTUAL_ENV && uv run mypy src/localmail              # expect clean, 106 files
cd gui && npm run check                                     # GUI svelte-check, 0 errors
```

### Try the new structured outcome (needs a running Ollama + a serve/MCP instance)
```bash
# HTTP: POST /v1/search; the response now has rewrite_status + rewrite_note.
curl -sk -X POST https://localhost:8443/v1/search \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"tax stuff from the accountant","smart":true}' \
  | jq '{rewrite_status, rewrite_note, rewrite_skipped}'
# A continuation page (re-send smart=true with the next_cursor) now reports
# rewrite_status="not_attempted" instead of a misleading rewrite_skipped=false.
# A missing Ollama model reports rewrite_status="failed" with an actionable
# rewrite_note naming the `ollama pull …` that fixes it.
```

`main` at `c91f2a8` (== `origin/main`). Branch `rewrite-outcome-status` @
`4f0873c` (pushed, PR #177 open). Latest migration `0027_import_jobs_owner.sql`;
next free slot `0028_*.sql`. **No migration this session.**
