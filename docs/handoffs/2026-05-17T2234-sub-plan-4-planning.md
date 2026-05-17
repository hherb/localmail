# NEXT_SESSION.md — localmail GUI client handoff

> **Delete this file once Sub-plans 4–5 are merged and the GUI is feature-complete.**

You're picking up after **Sub-plan 3 has merged (PR #20, merge commit `653c445`)** and the Sub-plan 4 plan has been drafted, committed to main (`ad99a4b`), and is ready to execute. The `gui-client-4` worktree is set up off `main` and waiting.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres. **Strictly read-only with respect to IMAP** — never sends/deletes/modifies upstream mail. Hybrid search (Phases 1 + 2 incl. attachment text) shipped. GUI server (`localmail serve`, migration 0014) shipped. GUI client Sub-plans 1–3 shipped (scaffolding, connection layer, 3-pane main view shell). See [CLAUDE.md](CLAUDE.md) and [docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What's done

| Component | Status |
|---|---|
| **Server**: `localmail.api` + FastAPI `localmail serve` + migration 0014 + CLI commands | ✅ shipped — merged via PR #6 into `worktree-phase2-hybrid-search` |
| **Sub-plan 1**: Tauri 2 + Svelte 5 scaffolding | ✅ shipped — PR #14 → `main` |
| **Sub-plan 2**: Connection core (TOFU pin + keyring + Connect/Login/AuthShell) | ✅ shipped — PR #19 → `main` |
| **Sub-plan 3**: 3-pane main view shell (plain-text bodies) | ✅ shipped — PR #20 → `main` (merge `653c445`) |
| **Sub-plan 4 plan**: Search + HTML body + attachments + server filter wiring | ✅ drafted + committed to `main` (`ad99a4b`); ready to execute |

## What this session did (commits)

| Commit | What |
|---|---|
| `ad99a4b` | `docs(gui-client): Sub-plan 4 implementation plan (search + HTML + attachments)` — 3635-line plan covering Phase A (server) + Phase B (client), 22 tasks total, TDD throughout. Located at [docs/superpowers/plans/2026-05-17-localmail-gui-client-4-search-html-attachments.md](docs/superpowers/plans/2026-05-17-localmail-gui-client-4-search-html-attachments.md). |

Also: synced local `main` with `origin/main` (fast-forward over the merge of PR #20). Set up `.claude/worktrees/gui-client-4` off `main` (branch `gui-client-4`).

Nothing pushed to remote this session (the plan commit is local on `main`).

## Sub-plan 4 — what to execute next

**Two phases, two worktrees, two PRs. Phase A must land first** (or the client's Task B10 fails its smoke test with `ValidationFailed: filter 'account_ids' is accepted by the API schema but not yet wired through to the search backend`).

### Phase A — server-side filter wiring

**Worktree:** `/Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search` (already exists).

5 tasks (A0–A5):
- A0: worktree verification
- A1: extend `SearchFilters` with `account_ids: list[int] | None` + `folder_ids: list[int] | None`
- A2: extend `parse_query` to recognise `account_id:NUM` / `folder_id:NUM` DSL tokens
- A3: extend `arms._filter_sql` with new ID-keyed predicates (`m.account_id = ANY(%s)`, `ml.mailbox_id = ANY(%s)`)
- A4: remove `account_ids` / `folder_ids` from `_KNOWN_UNSUPPORTED_FILTER_KEYS` in `api/search.py`; emit new DSL tokens
- A5: push branch + open PR against `worktree-phase2-hybrid-search`

**Acceptance:** `pytest tests/test_query_account_folder_id_tokens.py` (6 passes), `pytest tests/test_arms_id_filters.py` (4 passes), `pytest tests/test_api_search.py` (full suite + 4 new positive tests), `pytest` (full Phase A suite green), Phase A PR open.

### Phase B — client (search + HTML + attachments + tree wiring)

**Worktree:** `/Users/hherb/src/localmail/.claude/worktrees/gui-client-4` (already created off `main`, HEAD `653c445`, branch `gui-client-4`).

17 tasks (B0–B17). Tasks B0, B1, B2, B3, B4, B5, B6, B7, B8, B9, B11, B12, B13, B14, B15, B16, B17 are independent of Phase A. **Task B10** (AccountTree → server-side search dispatch) hard-depends on Phase A being applied to the server the smoke test hits — its unit tests pass without Phase A, but the manual smoke fails.

**Acceptance:** `cargo test` (40 prev + ~10 new = ~50 passes), `npm test` (48 prev + ~30 new = ~78 passes), `npm run check` (0 errors), Manual smoke per `gui/README.md` "Sub-plan 4 acceptance" (14 steps).

### How to start

```bash
# Sub-plan execution uses superpowers:subagent-driven-development (recommended)
# or superpowers:executing-plans. Either way the plan lives at:
#   docs/superpowers/plans/2026-05-17-localmail-gui-client-4-search-html-attachments.md

# Phase A first (or in parallel with Phase B's independent tasks):
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
git status            # confirm clean
git log --oneline -1  # confirm HEAD on worktree-phase2-hybrid-search

# Phase B (parallel-safe except Task B10):
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-4
git log --oneline -1  # confirm HEAD at 653c445
cd gui/src-tauri && cargo test 2>&1 | tail -5    # confirm 40 passing
cd .. && npm install && npm test 2>&1 | tail -10 # confirm 48 passing
```

Then invoke `superpowers:subagent-driven-development` and point it at the plan. A typical execution will dispatch 4–6 subagents in sequence over the course of the session.

## Key design decisions locked in this session (don't second-guess them)

1. **Server wiring: extend the DSL with `account_id:` / `folder_id:` tokens**, not a structured filters arg on `Searcher.search()` and not name resolution in the API layer. Smallest change; touches `query.py`, `arms.py`, `api/search.py` only; preserves the Searcher's public surface; no extra DB read per request.
2. **HTML body rendering: sandboxed iframe with `srcdoc` and per-iframe `<meta http-equiv="Content-Security-Policy">`**. App-level CSP stays strict; the iframe is the only place HTML email is rendered.
3. **PDF preview: bundle Mozilla pdf.js standalone (`pdfjs-dist`)**, lazy-imported only when the user opens a PDF.
4. **Tree narrowing: dispatch `/v1/search` with empty `query=""` + `account_ids` / `folder_ids`** filters. Replaces the Sub-plan 3 client-side filter over the 200-message `/v1/changes` load.

## Open decisions / risks for Sub-plan 4

1. **Phase A PR base branch.** The plan writes `--base worktree-phase2-hybrid-search`. If the phase 2 branch has been merged to main by the time Phase A executes, retarget to `main`. The plan flags this.
2. **`@tauri-apps/plugin-dialog` capability config.** Task B14 may need to add a capability entry in `gui/src-tauri/capabilities/default.json` (or wherever the connection-layer capabilities live) so the save dialog works at runtime. Unit tests mock the dialog so they pass either way; only the manual smoke catches this.
3. **`AuthError` may not yet have `Http(String)` / `Io(String)` variants.** Task B13 says "if it doesn't exist, add it". Doublecheck first to avoid duplicate variants.
4. **PDF.js worker URL bundling.** The plan uses Vite's `?url` import for the worker. If `tauri build` ever can't find the worker, the fix is `tauri.conf.json` → bundle resources, NOT disabling the worker (that locks the UI).
5. **Sub-plan 4 might be split into 4a (search + tree wiring) and 4b (HTML body + attachments)** if the PR comes out too large for a single review. Recommend keeping unified until PR diff size becomes a real problem.

## Known gotchas (still load-bearing — don't repeat them)

All the gotchas from prior handoffs still apply. New from this planning session:

- **`#state` is a true ES private field** in `MailStore` / `SearchStore` — invisible at runtime. Tests that want to populate state without calling `submit()` must use an exported `__setSearchResultsForTest`-style helper (see Task B9 in the plan) or mutate via the `snapshot` getter (which works because `snapshot` returns the rune-backed object directly).
- **`SearchFilters.accounts` vs `SearchFilters.account_ids`** in the server: `accounts` is the resolved-from-names list the Searcher fills in via `_resolve_account_names()`; `account_ids` is the new ID-direct list the API populates without name resolution. Both work; `_filter_sql` ORs the two (`if filters.accounts:` then `if filters.account_ids:` — separate `m.account_id = ANY(%s)` clauses). Two clauses ANDed against each other on `account_id` will narrow correctly (intersection), but the typical call sets only one.
- **`_filter_sql` `folder_ids` predicate omits the `mailboxes` join** — since we have the mailbox PKs directly, we filter on `message_labels.mailbox_id = ANY(%s)` without joining to `mailboxes`. The name-keyed `folders` predicate still needs the join (to `mb.name`).
- **`{@const C = mod.default}<C />`** is the runes-compatible pattern for dynamic component selection. `<svelte:component>` is deprecated and will warn.

## File map (Sub-plan 4 — quick reference)

```
docs/superpowers/specs/2026-05-17-localmail-gui-design.md           # design spec (all 5 sub-plans)
docs/superpowers/plans/2026-05-17-localmail-gui-client-4-search-html-attachments.md  # THE PLAN — 22 tasks
docs/superpowers/plans/2026-05-17-localmail-gui-client-{1,2,3}-*.md  # executed plans

.claude/worktrees/
  phase2-hybrid-search/                                             # Phase A worktree
  gui-client-4/                                                     # Phase B worktree (HEAD 653c445)

src/localmail/                                                      # Python (server)
  search/
    query.py        # ← Phase A A1+A2 extends SearchFilters + parse_query
    arms.py         # ← Phase A A3 extends _filter_sql with ID predicates
  api/
    search.py       # ← Phase A A4 removes account_ids/folder_ids rejection

gui/                                                                # Tauri + Svelte client (Phase B)
  src-tauri/src/commands/
    search.rs       # NEW (Task B1) — /v1/search wrapper
    attachments.rs  # NEW (Task B13/B15) — download + fetch bytes
  src/
    lib/
      api/search.ts          # NEW (B2) — SearchRequest, SearchResponse, SearchFiltersUI
      filter_parse.ts        # NEW (B3) — extractDslFilters, formatDslTokens
      snippet_sanitize.ts    # NEW (B4) — sanitizeSnippet (preserves <mark>)
      stores/search.svelte.ts # NEW (B5) — rune-backed search singleton
    components/
      SearchBar.svelte               # NEW (B6)
      FilterPopover.svelte           # NEW (B7) — lazy-imported by SearchBar
      ActiveFilterChips.svelte       # NEW (B8)
      HtmlBody.svelte                # NEW (B11) — sandboxed iframe srcdoc + CSP
      AttachmentRow.svelte           # NEW (B14)
      AttachmentsStrip.svelte        # NEW (B14)
      AttachmentPreviewModal.svelte  # NEW (B15) — image + lazy-PDF
      MessageList(.svelte/Row.svelte) # MODIFIED (B9) — snippet rendering
      AccountTree.svelte             # MODIFIED (B10) — dispatch server-side search
      ReadingPane.svelte             # MODIFIED (B12) — body-mode toggle + attachments strip
    screens/MainView.svelte          # MODIFIED (B16) — mount SearchBar + chips
```

Good luck. The plan is bite-sized and TDD throughout — a fresh subagent per task should walk through cleanly. Phase A is short (5 tasks, ~30 minutes of execution); Phase B is the bulk of the work (~3 hours, subagent-dispatched in parallel where possible).
