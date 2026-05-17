# NEXT_SESSION.md — localmail GUI client handoff

> **Delete this file once Sub-plan 5 ships and the GUI is feature-complete.**

You're picking up after **Sub-plan 4 has been fully implemented and pushed**:
- Phase A (server-side filter wiring) shipped to `worktree-phase2-hybrid-search` (4 commits).
- Phase B (GUI search + HTML body + attachments) shipped as **PR #21** against `main` (19 commits).

PR #21 is the next gate — once the user smoke-tests it against a Phase-A-enabled server and merges, only Sub-plan 5 (packaging + polish) remains.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres. **Strictly read-only with respect to IMAP**. Hybrid search (Phases 1 + 2 incl. attachment text) shipped. GUI server (`localmail serve`, migration 0014) shipped. GUI client Sub-plans 1–4 shipped. See [CLAUDE.md](CLAUDE.md) and [docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md).

## What's done

| Component | Status |
|---|---|
| **Server**: `localmail.api` + FastAPI `localmail serve` + migration 0014 + CLI commands | ✅ shipped — PR #6 into `worktree-phase2-hybrid-search` |
| **Sub-plan 1**: Tauri 2 + Svelte 5 scaffolding | ✅ shipped — PR #14 → `main` |
| **Sub-plan 2**: Connection core (TOFU pin + keyring + Connect/Login/AuthShell) | ✅ shipped — PR #19 → `main` |
| **Sub-plan 3**: 3-pane main view shell (plain-text bodies) | ✅ shipped — PR #20 → `main` (`653c445`) |
| **Sub-plan 4 — Phase A**: Server filter wiring (`account_ids` / `folder_ids`) | ✅ shipped — 4 commits on `worktree-phase2-hybrid-search` (`c046744`…`fa6c6d5`); no separate PR (committed directly on the long-lived integration branch) |
| **Sub-plan 4 — Phase B**: GUI search + HTML body + attachments | 🟡 **PR #21 open** — `gui-client-4` → `main`, 19 commits |

## What this session did

### Sub-plan 4 planning + execution

Drafted [docs/superpowers/plans/2026-05-17-localmail-gui-client-4-search-html-attachments.md](docs/superpowers/plans/2026-05-17-localmail-gui-client-4-search-html-attachments.md) (22 tasks, ~3600 lines) and executed it via `superpowers:subagent-driven-development` — one fresh sonnet subagent per task, controller verification inline rather than full reviewer subagents per task to keep per-task cost proportional to scope.

### Phase A — `worktree-phase2-hybrid-search`

| SHA | What |
|---|---|
| `c046744` | `feat(search): add account_ids/folder_ids fields to SearchFilters` |
| `08ffffe` | `feat(search): parse account_id: and folder_id: DSL tokens` |
| `0f76eeb` | `feat(search): _filter_sql ID-keyed predicates for account_ids/folder_ids` |
| `fa6c6d5` | `feat(api): enable account_ids/folder_ids filter forwarding` |

Tests: 45/45 across `test_query_parser`, `test_query_account_folder_id_tokens` (6 new), `test_arms`, `test_arms_id_filters` (4 new), `test_api_search` (5 new positive + adjusted negative), `test_serve_search_route` (+1 e2e).

**Branch pushed** to `origin/worktree-phase2-hybrid-search`. No separate PR — commits landed directly on the long-lived integration branch (same target as the GUI server PR #6 had been merged into).

### Phase B — `gui-client-4`

| SHA | What |
|---|---|
| `4150746` | `chore(gui-client): add pdfjs-dist for in-app PDF preview` |
| `69d2ecf` | `feat(gui-client): Rust /v1/search command + types` |
| `b9f2122` | `feat(gui-client): TS types + invoke wrapper for /v1/search` |
| `a4fe4a9` | `feat(gui-client): DSL ↔ structured filter round-trip helpers` |
| `e06865b` | `feat(gui-client): minimal allowlist sanitizer for server snippet_html` |
| `dcbec2c` | `feat(gui-client): search store singleton (rune state)` |
| `8e71356` | `feat(gui-client): SearchBar component with Enter/button submit` |
| `3163699` | `feat(gui-client): FilterPopover wired into SearchBar` |
| `21cfd08` | `feat(gui-client): ActiveFilterChips component with × clear` |
| `81a9b84` | `feat(gui-client): MessageList renders search results + snippets` |
| `17453f4` | `feat(gui-client): AccountTree drives server-side search on selection` |
| `e63a83e` | `feat(gui-client): HtmlBody — sandboxed iframe srcdoc with per-iframe CSP` |
| `39c18e8` | `feat(gui-client): ReadingPane HTML/Plain/Raw toggle + Load images` |
| `989c046` | `feat(gui-client): Rust /v1/attachments/{sha256} download command` |
| `b323bb9` | `feat(gui-client): attachments strip with per-row download` |
| `c90525e` | `feat(gui-client): AttachmentPreviewModal for images + PDFs` |
| `54588dc` | `feat(gui-client): mount SearchBar + ActiveFilterChips in MainView` |
| `6c0a84e` | `docs(gui-client): Sub-plan 4 manual smoke acceptance steps` |

Tests: cargo 49/49 (40 prev + 9 new), npm 101/101 (48 prev + 53 new), svelte-check 0 errors. Branch pushed; **PR #21**: <https://github.com/hherb/localmail/pull/21>.

### Notable adjustments made during execution

- The plan referenced `tests/test_query.py` — actual file is `tests/test_query_parser.py`. Subagents adapted.
- The plan's seed SQL for `tests/test_arms_id_filters.py` used wrong column names (`address`/`host`/`port` vs actual `email_address`/`imap_host`/`auth_method`). Subagent rewrote the seed to match real schema.
- `SearchFiltersWire` / `SearchRequest` Rust structs needed `Deserialize` (not just `Serialize`) — Tauri command argument deserialization requires it. Plan had `Serialize` only; subagent caught + fixed.
- `AuthError` lacked an `Io(String)` variant. Added one in B13.
- `@tauri-apps/plugin-dialog` + `tauri-plugin-dialog` crate needed installation and capability config — added cleanly: `"dialog:default"` to `gui/src-tauri/capabilities/default.json` alongside the existing `core:default` / `shell:allow-open`.
- The test runtime lacks `@testing-library/jest-dom` — all new tests use `toBeTruthy()` instead of `toBeInTheDocument()`. Documented for future tasks.
- `snippet_sanitize.ts` needed paired-tag counting so that unpaired `</mark>` from attribute-bearing `<mark style="x">…</mark>` falls through to escaping rather than being placeholder-swapped.
- `MessageListRow.svelte` was refactored from `message: MessageSummary` prop to flat props (`subject`, `from`, `date`, `account`, `snippet`, `selected`, `onSelect`) to support snippet rendering. Only `MessageList.svelte` consumes it.

## What remains — Sub-plan 5

**Worktree to create**: `gui-client-5` off `main` (after PR #21 merges).

Scope (from the design spec):
- Branded icons; `.dmg` / `.msi` / `.AppImage` bundles via `npm run tauri build`.
- Version-mismatch handling (hard modal when server `api_major` ≠ client expected).
- Background change polling on the active view (poll `/v1/changes?since=…` every 30s).
- Resizable splitter (column widths).
- Settings screen (HTML image policy, density, page size, debug toggle, change password, view logs).
- Header-unfold widget + `?headers=full` lazy fetch in ReadingPane.
- Raw RFC822 view (currently a placeholder in the body-mode toggle).
- Search debug pane (per-arm scores, matched-chunk highlighting).
- Multi-page paginated PDF preview (currently page 1 only).
- `date_from` / `date_to` / `lang` server-side filter forwarding (currently the popover writes equivalent `after:` / `before:` DSL tokens which are end-to-end supported, but `date_from`/`date_to`/`lang` keys still get rejected by `api/search.py`).
- Server-side `/v1/folders/{id}/messages` endpoint (currently the GUI relies on `/v1/search` with empty query + filter, which works but is slightly more expensive than a dedicated list endpoint would be).

### How to start Sub-plan 5

```bash
cd /Users/hherb/src/localmail
# After PR #21 merges:
git checkout main && git pull
git worktree add .claude/worktrees/gui-client-5 -b gui-client-5 main
cd .claude/worktrees/gui-client-5
# Invoke superpowers:writing-plans for Sub-plan 5
```

## Known gotchas (still load-bearing — don't repeat them)

All the gotchas from prior handoffs still apply. New from this session:

- **`@testing-library/jest-dom` is NOT installed in `gui/`.** Use `toBeTruthy()` / `toBeFalsy()` in component tests, never `toBeInTheDocument()`. Pattern: `gui/src/components/AccountTree.test.ts`.
- **Rust structs used as Tauri command arguments need `#[derive(Deserialize)]`** even if they're "output" types — Tauri deserializes them off the JS-side `invoke()` call.
- **`SearchFilters.accounts` vs `SearchFilters.account_ids`** in the server: `accounts` is resolved-from-names by the Searcher; `account_ids` is direct integer PKs from the API. Both produce `m.account_id = ANY(%s)` predicates; setting both narrows by intersection (ANDed).
- **`_filter_sql` `folder_ids` predicate omits the `mailboxes` join** — uses `message_labels.mailbox_id = ANY(%s)` directly. The name-keyed `folders` predicate still needs the join (to `mb.name`).
- **Plan-snippet schema may not match real schema** — when a plan provides seed SQL, verify against `migrations/` before running. The plan's `tests/test_arms_id_filters.py` seed had to be rewritten for column-name mismatches.
- **Per-row chip key uses random fallback**: `{#each atts as a (a.sha256 ?? a.filename ?? Math.random())}` — fine for v1, but if two attachments share neither sha256 nor filename you get re-mounts on every render. Edge case; not worth fixing in v1.
- **`pdfjs-dist/build/pdf.worker.mjs?url`** import works under Vite for both `npm test` (vitest) and `tauri dev`. If `tauri build` ever can't find the worker, the fix is `tauri.conf.json` → bundle resources, NOT disabling the worker (locks the UI).
- **`bodyMode` is sticky across messages; `externalImagesAllowed` resets per-message.** Both live on `mail.svelte.ts`'s `MailState`.
- **Sub-plan 4's manual smoke requires Phase A on the server.** If the chip "ValidationFailed: filter 'account_ids' is accepted by the API schema…" appears in the GUI, the server it's hitting hasn't been rebuilt against `worktree-phase2-hybrid-search` post-A1–A4.

## File map (after Sub-plan 4)

```
docs/superpowers/specs/2026-05-17-localmail-gui-design.md           # design spec (all 5 sub-plans)
docs/superpowers/plans/2026-05-17-localmail-gui-client-{1,2,3,4}-*.md  # plans

.claude/worktrees/
  phase2-hybrid-search/    # Phase A landed here (no separate PR; commits on integration branch)
  gui-client-{2,3,4}/      # Sub-plan worktrees (4 is the one with the open PR #21)

src/localmail/                                                      # Python (server)
  search/
    query.py        # Phase A: SearchFilters.account_ids/folder_ids + parse_query tokens
    arms.py         # Phase A: _filter_sql ID-keyed predicates
  api/
    search.py       # Phase A: emits account_id:/folder_id: DSL tokens

gui/                                                                # Tauri + Svelte client
  src-tauri/src/commands/
    search.rs       # Task B1 — /v1/search wrapper
    attachments.rs  # Tasks B13/B15 — download + fetch bytes
  src/
    lib/
      api/search.ts          # B2 — SearchRequest, SearchResponse, SearchFiltersUI
      filter_parse.ts        # B3 — extractDslFilters, formatDslTokens
      snippet_sanitize.ts    # B4 — sanitizeSnippet (preserves <mark>)
      stores/search.svelte.ts # B5 — rune-backed search singleton (+ test helper)
    components/
      SearchBar.svelte               # B6 — lazy-imports FilterPopover
      FilterPopover.svelte           # B7
      ActiveFilterChips.svelte       # B8
      MessageList(.svelte/Row.svelte) # B9 — snippet rendering; row props flat-refactored
      AccountTree.svelte             # B10 — dispatches /v1/search on selection
      HtmlBody.svelte                # B11 — sandboxed iframe srcdoc + CSP
      ReadingPane.svelte             # B12 — HTML/Plain/Raw toggle + Load images + attachments strip
      AttachmentRow.svelte           # B14
      AttachmentsStrip.svelte        # B14 — lazy-imports AttachmentPreviewModal
      AttachmentPreviewModal.svelte  # B15 — image + lazy-PDF
    screens/MainView.svelte          # B16 — mounts SearchBar + chips above the panes
```

Good luck. PR #21 is the gate — once it merges with Phase A live on the server, Sub-plan 5 (packaging + polish) is the only remaining sub-plan.
