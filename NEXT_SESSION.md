# NEXT_SESSION.md — localmail GUI client handoff

> **Delete this file once Sub-plans 4–5 are merged and the GUI is feature-complete.**

You're picking up after Sub-plan 3 (Main view shell) has been **implemented, code-reviewed, and the review findings addressed and pushed**. PR #20 is open against `main` and is the last thing standing between you and starting Sub-plan 4.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres. **Strictly read-only with respect to IMAP** — never sends/deletes/modifies upstream mail. The user (Horst Herb) has a live archive of ~30k chunked + embedded messages. Hybrid search subsystem (Phase 1 + Phase 2 with attachments) is shipped. See [CLAUDE.md](CLAUDE.md) for full guidance and [docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md) for the GUI design spec.

## What's done

| Component | Status |
|---|---|
| **Server**: `localmail.api` library + FastAPI `localmail serve` + migration 0014 + CLI commands | ✅ shipped — merged via PR #6 into `worktree-phase2-hybrid-search` |
| **Sub-plan 1**: Tauri 2 + Svelte 5 scaffolding (greet roundtrip) | ✅ shipped — merged via PR #14 into `main` |
| **Sub-plan 2**: Connection core — Rust HTTP + TOFU pin + keyring + ConnectScreen/LoginScreen/AuthShell | ✅ shipped — merged via PR #19 into `main` |
| **Sub-plan 3**: 3-pane main view shell (plain-text bodies) | ✅ code complete on `gui-client-3`, all tests green, **code review done, fixes pushed, PR #20 open** |

## Where things stand at session end (2026-05-17)

**Branch `gui-client-3`** is at `c10e576` with:
- 12 feature commits from Sub-plan 3 (Rust commands + Svelte components + store + screen)
- 1 fix commit (`c10e576`) addressing all 7 findings from the `/review` round on PR #20

**PR #20**: <https://github.com/hherb/localmail/pull/20> — `feat(gui-client): Sub-plan 3 — Main view shell (3-pane)`

**Test counts after review fixes**: cargo `40 passing` (was 33; +7 from new `commands::session` module and `ChangesResponse` deserialisation tests), npm `48 passing` (was 44; +4 from MessageList error branch, AccountTree race regression, selectionMatches folder, null next_cursor), svelte-check `0 errors`.

### What the review-fix commit changed

| # | Issue from review | Fix |
|---|---|---|
| 1 | `next_cursor` declared as non-optional `String` would fail every initial `/v1/changes` deserialisation | `Option<String>` in Rust, `string \| null` in TS; 3 new deserialisation tests |
| 2 | `read_connection` helper duplicated verbatim in 4 command files | New `gui/src-tauri/src/commands/session.rs` exporting `read_endpoint` + `read_authenticated`; `auth.rs`, `accounts.rs`, `changes.rs`, `messages.rs` all use them |
| 3 | `AccountTree.selectAccount` race: rapid double-click collapsed the tree mid-load | New `expansionsInFlight` Set guards re-entry; regression test added |
| 4 | `MessageList` `.error` branch was untested | Added "renders error message when store.errorMessage is set after a failed load" |
| 5 | `selectionMatches` "folder" test only varied `accountId`, not `folderId` | Added test asserting `folderId` varying is irrelevant (documents the deferred behaviour) |
| 6 | Imports added at the *bottom* of `gui/src/lib/tauri.ts` | Moved to top with the other imports |
| 7 | `vitePreprocess({ style: false })` workaround had no explanation | Added 6-line comment explaining the Vite 6 vitest bug + when to revert |

## What remains

| Sub-plan | Scope | Acceptance criteria |
|---|---|---|
| **3 (finish)** | Manual smoke (if not done yet) + merge PR #20 | All 10 steps in `gui/README.md` "Manual smoke (Sub-plan 3 acceptance)" pass on the user's machine; PR #20 merged to `main` |
| **4: Search + reading polish** | Search bar; filter popover with DSL parity (`from:`, `to:`, `subject:`, `after:`, `before:`, `has:attachment`); snippet rendering with `<mark>` highlighting; sanitized HTML body rendering inside iframe srcdoc (CSP-isolated); attachments strip + per-attachment download + preview modal (PDF.js + img); wire `account_ids` / `folder_ids` filters from the tree through to `/v1/search` (server-side narrowing — currently raises ValidationFailed) | Search bar submits to `/v1/search`; results render with snippets; HTML toggle works in reading pane without breaking CSP; attachments downloadable; selecting an account or folder in the tree narrows search results on the server, not just the loaded 200 |
| **5: Packaging + polish** | Branded icons; `.dmg`/`.msi`/`.AppImage` bundles; version-mismatch handling; background change polling on the active view; resizable splitter | `npm run tauri build` produces a signed-or-unsigned `.dmg` on macOS; opening on a fresh machine works end-to-end; client surfaces a hard modal when server `api_major` ≠ client expected; polls `/v1/changes?since=…` every 30s |

## How to continue (next session)

1. **Check PR #20 status** — if not merged, do manual smoke (see commands below). If smoke passes, ask the user to merge (or merge it yourself if they say go). If review left additional comments since the last fix commit, address those first.
2. **After PR #20 merges**: pull `main`, create the Sub-plan 4 worktree, and draft the plan with `superpowers:writing-plans`.

### Manual smoke for PR #20 (if not yet done)

```bash
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-3
git log --oneline -3                              # confirm HEAD is c10e576 or later
cd gui/src-tauri && cargo test                    # expect 40 passing
cd .. && npm test                                 # expect 48 passing
cd .. && npm run check                            # expect 0 errors

# In one terminal — run the server (from phase2 worktree):
cd /Users/hherb/src/localmail/.claude/worktrees/phase2-hybrid-search
unset VIRTUAL_ENV && uv run localmail serve --bind 127.0.0.1

# In another — launch the GUI:
cd /Users/hherb/src/localmail/.claude/worktrees/gui-client-3/gui
npm run tauri dev

# Then walk through gui/README.md → "Manual smoke (Sub-plan 3 acceptance)" steps 1–10
```

### After PR #20 merges — start Sub-plan 4

```bash
cd /Users/hherb/src/localmail
git checkout main && git pull
git worktree add .claude/worktrees/gui-client-4 -b gui-client-4 main
cd .claude/worktrees/gui-client-4
# Invoke superpowers:writing-plans for Sub-plan 4
```

## Open decisions / risks for Sub-plan 4

1. **Server-side narrowing wiring (must come first).** `/v1/search` currently raises `ValidationFailed` for `account_ids` and `folder_ids` filters (see `src/localmail/api/search.py` on `worktree-phase2-hybrid-search` — the `_KNOWN_UNSUPPORTED_FILTER_KEYS` constant). Sub-plan 4 must (a) remove those keys from the unsupported set and (b) wire them through to the underlying `Searcher` via the filter DSL or a new arg. This is a **server-side change** that needs to land in the phase2 worktree before the GUI side can use it.
2. **HTML rendering CSP.** The GUI's app-level CSP is strict. Sub-plan 4 must use `<iframe srcdoc>` with its own restrictive CSP for email HTML; do **not** loosen the app CSP. `gui/README.md` "Security baseline" spells this out.
3. **PDF preview library.** Spec mentions PDF.js. Decide between bundling Mozilla's pdf.js standalone (~2MB extra) vs a Tauri plugin. Recommendation: bundle standalone — Tauri's bundler handles static assets well and offline-first is a project goal.
4. **Workaround left in place that may be revisitable**: `gui/svelte.config.js` uses `vitePreprocess({ style: false })` to dodge a Vite 6 / vite-plugin-svelte vitest bug (PartialEnvironment proxy crash inside `preprocessCSS`). The file now has an explanatory comment. If a future Vite or vite-plugin-svelte release fixes it, revert.

## Suggested approach for Sub-plan 4

- Use `superpowers:writing-plans` to draft. Reference [docs/superpowers/plans/2026-05-17-localmail-gui-client-3-mainview.md](docs/superpowers/plans/2026-05-17-localmail-gui-client-3-mainview.md) for format (it's the most recent and well-shaped one).
- Use `superpowers:subagent-driven-development` for execution — the pattern worked for Sub-plan 3 (4 subagents, ~3 hours total).
- The Sub-plan 4 plan must start with **server-side tasks** (wiring `account_ids` / `folder_ids` through to the searcher in the `phase2-hybrid-search` worktree) before the client-side filter UX can be smoke-tested end-to-end.
- The new `commands/session.rs` helpers (`read_endpoint`, `read_authenticated`) are the canonical way to read keyring state in new commands. **Don't reintroduce the inlined `read_connection` pattern** — that's what the review fixed.

## Known gotchas (still load-bearing — don't repeat them)

All the gotchas from prior handoffs still apply (Tauri rebuild on icon change, macOS panic hook, rustls crypto provider, keyring mock useless, `vi.hoisted` for mocks, `<svelte:component>` deprecated in Svelte 5 runes mode, error serialisation nested, `unset VIRTUAL_ENV` for Python). See `git log -- NEXT_SESSION.md` for prior versions if you need the full list.

**New from this session (code review round):**
- **Cursor-pagination response fields are usually nullable.** When wrapping any new `/v1/...` endpoint that returns a `next_cursor` (or similar), model it as `Option<String>` / `string | null` from the start. Don't assume the server always sends a value.
- **For shared per-command setup (keyring reads, etc.), put it in `commands/session.rs`** rather than copy/pasting per file. The pattern is now established with `read_endpoint` and `read_authenticated`.
- **Component error branches need their own test.** Setting `errorMessage` in the store and asserting on `snapshot.errorMessage` is not the same as rendering the component and asserting the `.error` div appears. Both matter.

## File map (quick reference for Sub-plan 4)

```
docs/superpowers/specs/2026-05-17-localmail-gui-design.md            # design spec (all 5 sub-plans)
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
      session.rs        # NEW — read_endpoint, read_authenticated (review fix)
  src/
    App.svelte          # just <Router />
    routes/Router.svelte
    screens/
      ConnectScreen.svelte
      LoginScreen.svelte
      MainView.svelte           # 3-pane shell (Sub-plan 3)
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
        mail.svelte.ts  # mail browsing state (Sub-plan 3)
```

Good luck. PR #20 is the gate — once it merges, Sub-plan 4 is unblocked and the search bar / HTML rendering / attachments work begins.
