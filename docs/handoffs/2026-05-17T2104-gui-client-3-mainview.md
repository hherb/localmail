# NEXT_SESSION.md — localmail GUI client handoff

> **Delete this file once Sub-plans 4–5 are merged and the GUI is feature-complete.**

You're picking up after Sub-plan 3 (Main view shell) has been **implemented locally on branch `gui-client-3` but not yet manually smoked or merged**. Sub-plans 1, 2, 3 are code-complete; Sub-plans 4 and 5 remain.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres. **Strictly read-only with respect to IMAP** — never sends/deletes/modifies upstream mail. The user (Horst Herb) has a live archive of ~30k chunked + embedded messages. Hybrid search subsystem (Phase 1 + Phase 2 with attachments) is shipped. See [CLAUDE.md](CLAUDE.md) for full guidance and [docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md) for the GUI design spec.

## What's done

| Component | Status |
|---|---|
| **Server**: `localmail.api` library + FastAPI `localmail serve` + migration 0014 + CLI commands | ✅ shipped — merged via PR #6 into `worktree-phase2-hybrid-search` |
| **Sub-plan 1**: Tauri 2 + Svelte 5 scaffolding (greet roundtrip) | ✅ shipped — merged via PR #14 into `main` |
| **Sub-plan 2**: Connection core — Rust HTTP + TOFU pin + keyring + ConnectScreen/LoginScreen/AuthShell | ✅ shipped — merged via PR #19 into `main` |
| **Sub-plan 3**: 3-pane main view shell (plain-text bodies) | ✅ code complete on `gui-client-3`, all tests green, **pending manual smoke + PR** |

## What we shipped this session (2026-05-17)

Branch: `gui-client-3` (off `main`), 12 commits on top of `857319a`:

| SHA | Subject |
|---|---|
| `342ec91` | docs(gui-client): Sub-plan 3 implementation plan (Main view shell) |
| `1363531` | feat(gui-client): Rust commands for /v1/accounts and /v1/accounts/{id}/folders |
| `0956030` | feat(gui-client): Rust command for /v1/changes (recent messages) |
| `507520a` | feat(gui-client): Rust command for /v1/messages/{id} (full detail) |
| `91ca8fc` | feat(gui-client): TS types + invoke wrappers for accounts/folders/messages |
| `299d7b3` | feat(gui-client): pure format helpers (addressLabel, truncate, formatRelativeDate, selectionMatches) |
| `9d00918` | feat(gui-client): mail.svelte.ts singleton store + unit tests |
| `1fbb0f3` | feat(gui-client): AccountTree component + tests |
| `a5004e7` | feat(gui-client): MessageList + MessageListRow + tests |
| `c0dabd3` | feat(gui-client): ReadingPane component (plain-text only) + tests |
| `fa79b07` | feat(gui-client): MainView screen replaces AuthenticatedShell placeholder |
| `1999de5` | docs(gui-client): Sub-plan 3 manual smoke acceptance steps |

Final test counts: **33 Rust tests passing, 44 vitest tests passing, 0 type-check errors**.

## What remains

| Sub-plan | Scope | Acceptance criteria |
|---|---|---|
| **3 (finish)** | Manual smoke + open + merge PR | All 10 steps in `gui/README.md` "Manual smoke (Sub-plan 3 acceptance)" pass on the user's machine; PR opened against `main`; CI (if/when set up) green |
| **4: Search + reading polish** | Search bar; filter popover with DSL parity (`from:`, `to:`, `subject:`, `after:`, `before:`, `has:attachment`); snippet rendering with `<mark>` highlighting; sanitized HTML body rendering inside iframe srcdoc (CSP-isolated); attachments strip + per-attachment download + preview modal (PDF.js + img); wire `account_ids` / `folder_ids` filters from the tree through to `/v1/search` (server-side narrowing — currently raises ValidationFailed) | Search bar submits to `/v1/search`; results render with snippets; HTML toggle works in reading pane without breaking CSP; attachments downloadable; selecting an account or folder in the tree narrows search results on the server, not just the loaded 200 |
| **5: Packaging + polish** | Branded icons; `.dmg`/`.msi`/`.AppImage` bundles; version-mismatch handling; background change polling on the active view; resizable splitter | `npm run tauri build` produces a signed-or-unsigned `.dmg` on macOS; opening a `.dmg` on a fresh machine works end-to-end; client surfaces a hard modal when server `api_major` ≠ client expected; polls `/v1/changes?since=…` every 30s |

## Open decisions / risks for Sub-plan 4

1. **Server-side narrowing wiring.** The plan for Sub-plan 3 deliberately deferred this. `/v1/search` currently raises `ValidationFailed` for `account_ids` and `folder_ids` filters (see `src/localmail/api/search.py` on the `worktree-phase2-hybrid-search` branch — the `_KNOWN_UNSUPPORTED_FILTER_KEYS` constant). Sub-plan 4 must (a) remove those keys from the unsupported set and (b) wire them through to the underlying `Searcher` via the filter DSL or a new arg. This is a **server-side change** that needs to land in the phase2 worktree before the GUI side can use it.
2. **HTML rendering CSP.** The GUI's app-level CSP is strict. Sub-plan 4 must use `<iframe srcdoc>` with its own restrictive CSP for email HTML; do NOT loosen the app CSP. The `gui/README.md` "Security baseline" section spells this out.
3. **PDF preview library.** Spec mentions PDF.js. Decide between bundling (Mozilla's pdf.js, ~2MB extra) vs a Tauri plugin. Recommendation: bundle PDF.js standalone since Tauri's bundler handles static assets well and offline-first is a project goal.
4. **Sub-plan 3 deviations to revisit during PR review:**
   - `gui/svelte.config.js` was changed from `vitePreprocess()` to `vitePreprocess({ style: false })` to dodge a Vite 6 bug in the vitest pipeline (`PartialEnvironment` proxy crash inside `preprocessCSS`). Plain `<style>` blocks still work via Vite's native handling. If a future Vite or vite-plugin-svelte release fixes the bug, revert.
   - `gui/src/components/AccountTree.svelte` wraps the caret/icon/label triplet in separate `<span>` children so testing-library's `getByText("personal")` finds the label as its own text node. Visually identical; pure DOM shape change.

## How to continue (next session)

1. **Confirm with the user** whether to (a) manually smoke + PR Sub-plan 3 first, or (b) start drafting Sub-plan 4 immediately. Default: (a) — get Sub-plan 3 merged first to keep the branch graph clean.
2. **For manual smoke**, follow `gui/README.md` "Manual smoke (Sub-plan 3 acceptance)" steps 1–10 from inside the `.claude/worktrees/gui-client-3/gui/` directory.
3. **For Sub-plan 4**:
   - Use `superpowers:writing-plans` to draft. Reference [docs/superpowers/plans/2026-05-17-localmail-gui-client-3-mainview.md](docs/superpowers/plans/2026-05-17-localmail-gui-client-3-mainview.md) for format.
   - The plan must include **server-side work** (wiring `account_ids`/`folder_ids` through to the searcher in the `phase2-hybrid-search` worktree) before the client-side filter UX can be smoke-tested.
   - Use `superpowers:subagent-driven-development` for execution — pattern works (Sub-plan 3 used 4 subagents, ~3 hours total).
   - Worktree: `.claude/worktrees/gui-client-4`, branched off `main` (post-merge of Sub-plan 3).

## Exact commands to resume

### Pick up Sub-plan 3 for manual smoke + merge

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-3
git log --oneline -5                              # confirm HEAD is 1999de5
cd gui/src-tauri && cargo test                    # expect 33 passing
cd .. && npm test                                 # expect 44 passing
cd .. && npm run check                            # expect 0 errors

# In one terminal — run the server (from phase2 worktree):
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
unset VIRTUAL_ENV && uv run localmail serve --bind 127.0.0.1

# In another — launch the GUI:
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-3/gui
npm run tauri dev

# Then walk through gui/README.md → "Manual smoke (Sub-plan 3 acceptance)" steps 1–10
```

If smoke passes:

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-3
git push -u origin gui-client-3
gh pr create --base main --head gui-client-3 --title "feat(gui-client): Sub-plan 3 — Main view shell (3-pane)" --body "..."  # see plan Task 11 step 3 for body template
```

### Start Sub-plan 4 (after Sub-plan 3 merges)

```bash
cd /Users/hherb/src/localmail
git fetch && git checkout main && git pull
git worktree add .claude/worktrees/gui-client-4 -b gui-client-4 main
# then invoke superpowers:writing-plans for Sub-plan 4
```

## Known gotchas (still load-bearing — don't repeat them)

All the gotchas from the previous handoff still apply (Tauri rebuild on icon change, macOS panic hook, rustls crypto provider, keyring mock useless, vi.hoisted for mocks, `<svelte:component>` deprecated, error serialization nested, `unset VIRTUAL_ENV` for Python). See git history for the prior NEXT_SESSION.md if you need the full list — they did not change this session.

**New gotcha from this session:** when adding component tests with `@testing-library/svelte`, ensure `vite.config.ts` has the `svelteTesting()` plugin and `test.environment = "jsdom"`. The `svelte.config.js` style:false workaround above is Vite-6 specific and may not be needed after a future vite-plugin-svelte release.

## File map (quick reference for Sub-plan 4)

```
docs/superpowers/specs/2026-05-17-localmail-gui-design.md     # design spec (all 5 sub-plans)
docs/superpowers/plans/2026-05-17-localmail-gui-client-{1,2,3}-*.md  # executed plans

gui/                                                          # Tauri + Svelte client
  src-tauri/src/
    lib.rs              # tauri::Builder, panic hook, generate_handler! registers ALL cmds
    http/               # verifier (TOFU), client (reqwest helpers), errors
    storage/keyring.rs  # KeyringStore + KeyringBackend trait + MemKeyring fake
    commands/
      connect.rs        # probe_server, confirm_trust (Sub-plan 2)
      auth.rs           # login, logout, refresh, whoami (Sub-plan 2)
      capabilities.rs   # get_capabilities (Sub-plan 2)
      accounts.rs       # list_accounts, list_folders (Sub-plan 3)
      changes.rs        # list_recent_messages (Sub-plan 3)
      messages.rs       # get_message (Sub-plan 3)
  src/
    App.svelte          # just <Router />
    routes/Router.svelte
    screens/
      ConnectScreen.svelte
      LoginScreen.svelte
      MainView.svelte           # NEW — 3-pane shell (Sub-plan 3)
    components/
      AccountTree.svelte        # left rail
      MessageList.svelte        # middle pane
      MessageListRow.svelte
      ReadingPane.svelte        # right pane (plain-text only)
    lib/
      tauri.ts          # typed invoke() wrappers
      format.ts         # pure helpers (addressLabel, formatRelativeDate, truncate, selectionMatches)
      api/types.ts      # shared TS types matching Rust structs
      stores/
        auth.svelte.ts  # auth state machine (Sub-plan 2)
        mail.svelte.ts  # NEW — mail browsing state (Sub-plan 3)
```

Good luck. Sub-plan 3 is the visible turning point — once it merges, you have a real GUI to look at while planning Sub-plan 4.
