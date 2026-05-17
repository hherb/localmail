# NEXT_SESSION.md — localmail GUI client handoff

> **Delete this file once Sub-plans 3–5 are merged and the GUI is feature-complete.**

You're picking up mid-way through a 5-sub-plan build of the localmail GUI client (Tauri 2 + Svelte 5 + Rust). The server is shipped and merged. The client has scaffolding (Sub-plan 1) and full auth flow (Sub-plan 2) shipped. Sub-plans 3, 4, 5 remain.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres. **Strictly read-only with respect to IMAP** — never sends/deletes/modifies upstream mail. The user (Horst Herb) has a live archive of ~30k chunked + embedded messages. Hybrid search subsystem (Phase 1 + Phase 2 with attachments) is shipped. See [CLAUDE.md](CLAUDE.md) for full guidance and [docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md) for the GUI design spec.

## What's done

| Component | Status |
|---|---|
| **Server**: `localmail.api` library + FastAPI `localmail serve` + migration 0014 + CLI commands (add-api-user, serve, rotate-tls, etc.) | ✅ shipped — merged via PR #6 into `worktree-phase2-hybrid-search` |
| **Sub-plan 1**: Tauri 2 + Svelte 5 scaffolding at top-level `gui/` (greet roundtrip) | ✅ shipped — merged via PR #14 into `main` |
| **Sub-plan 2**: Connection core — Rust HTTP + TOFU pin + keyring + Svelte Connect/Login/AuthenticatedShell | ✅ working end-to-end, manual smoke confirmed; **PR pending merge** at session end |

## What remains

| Sub-plan | Scope | Status |
|---|---|---|
| 3: Main view shell | Layout-A 3-pane (account/folder tree · result list · reading pane); plain-text bodies only | ⏳ to plan + execute |
| 4: Search + reading polish | Search bar, filter popover with DSL parity, snippets, sanitized HTML rendering, attachments strip + preview | ⏳ to plan + execute |
| 5: Packaging + polish | Branded icons, `.dmg`/`.msi`/`.AppImage` bundles, version-mismatch handling, change polling | ⏳ to plan + execute |

## How to continue

1. **Confirm with the user** which sub-plan they want next. Default is Sub-plan 3, but they may want to skip ahead to packaging (Sub-plan 5) if they want to share a `.dmg` with someone.
2. **Use `superpowers:writing-plans`** to draft. Reference the existing plan format in `docs/superpowers/plans/2026-05-17-localmail-gui-client-{1,2}-*.md`. Same structure, same TDD discipline.
3. **Use `superpowers:subagent-driven-development` for execution** — one `sonnet` implementer per task. This is what Sub-plans 1 and 2 used; it works. Skip the formal spec/quality reviewer dispatches for tasks where the plan code is literal (saves significant tokens); do invoke them for tasks with judgment calls (e.g., the layout-A component split in Sub-plan 3).
4. **Worktree per sub-plan**, branched off `main`. Naming: `.claude/worktrees/gui-client-3`, `gui-client-4`, etc.
5. **Manual smoke at the end of each sub-plan** — user runs `npm run tauri dev` and verifies visually. Subagents can't do this. Document the acceptance steps in `gui/README.md`.

## Known gotchas (painful lessons — don't repeat them)

### Rust / Tauri / macOS

- **`tauri::generate_context!()` embeds icons at compile time.** Changing `gui/src-tauri/icons/*` doesn't trigger a cargo rebuild — cargo doesn't track those as source deps. After any icon change, touch `gui/src-tauri/src/lib.rs` to force a relink. (Documented in `gui/README.md`.)
- **macOS panic hook is essential.** `gui/src-tauri/src/lib.rs::run()` installs `panic::set_hook` because tao's macOS event loop runs inside `extern "C"` Obj-C callbacks that can't unwind across the FFI boundary — without the hook, panics show as the useless "panic in a function that cannot unwind" with the real message lost. **Do not remove it.**
- **CSP is strict** (`default-src 'self'; script-src 'self'; …`). Sub-plan 4 must render email HTML bodies — **do NOT loosen the app-level CSP**. Sandbox email HTML in an `<iframe srcdoc>` with its own restrictive CSP. The "Security baseline" section of `gui/README.md` explicitly notes this.

### Rust crates and APIs

- **rustls 0.23 + reqwest 0.12 with custom verifier**: working code is in `gui/src-tauri/src/http/client.rs::build_reqwest_with_verifier()`. The TOFU pin lives in `gui/src-tauri/src/http/verifier.rs::TofuVerifier` (two modes: `Probe` and `Pinned`).
- **rustls crypto provider is installed in `lib.rs::run().setup(...)`.** If you spawn TLS clients outside the Tauri runtime (e.g. unit tests), you must install the provider yourself or build the provider into the `ClientConfig` directly (the http::client helpers do the latter).
- **keyring 3.x `mock` backend has `EntryOnly` persistence and is useless as a test drop-in.** `gui/src-tauri/src/storage/keyring.rs` solves this with a `KeyringBackend` trait + `MemKeyring` HashMap-backed fake, both publicly exposed. Tests inject via `KeyringStore::with_backend(MemKeyring::new())`; production calls `KeyringStore::new()`. Reuse this pattern in any sub-plan that adds stateful tests.

### Svelte / vitest

- **Auth store is at `gui/src/lib/stores/auth.svelte.ts`** — the `.svelte.ts` extension is required for rune (`$state`, `$derived`) transformation in non-component files.
- **`vi.mock` hoists above const declarations.** Use `vi.hoisted(() => ({ mockA: vi.fn(), … }))` to declare mocks the factory can reference. See `auth.test.ts`.
- **`<svelte:component>` is deprecated in Svelte 5 runes mode.** Use `{@const C = component}<C />`.
- **Dynamic imports to not-yet-created files** need `// @ts-ignore` + `/* @vite-ignore */`. Used in `Router.svelte` for lazy-loaded LoginScreen/AuthenticatedShell.
- **Error serialization is nested.** Rust returns `ConnectError::Http(HttpError)` which serializes as `{kind: "Http", detail: {kind: "Network", detail: "..."}}`. The auth store's `formatError` recurses — preserve that for any new error display.

### Project setup gotchas

- **`unset VIRTUAL_ENV && uv run …`** for every Python command. Shells often have stale `VIRTUAL_ENV` from other projects.
- **Server runs from `worktree-phase2-hybrid-search` branch**, not main. `cd .claude/worktrees/phase2-hybrid-search` then `uv run localmail serve --bind 127.0.0.1`. (Eventually phase2 merges to main and this won't matter.)
- **Migrations are 0001–0014.** New schema goes in 0015+. **Never edit applied migrations** — add a new numbered file. `uv run localmail init-db` applies all pending; idempotent.
- **GUI client base is `main`** (not phase2). The client has no on-disk dependency on the Python server — it talks HTTPS at runtime.

## State at end of this session

- `main` includes Sub-plan 1 (Tauri+Svelte scaffolding) merge.
- `gui-client-2` branch + worktree at `.claude/worktrees/gui-client-2/` — Sub-plan 2 work; PR open.
- `worktree-phase2-hybrid-search` branch has server work + phase2 attachment search.
- Live `localmail` database migrated through 0014; `alice` API user exists.
- Manual end-to-end smoke test passed: Connect → TOFU → Login → AuthenticatedShell renders alice + capabilities (search ✓, attachments ✓, attachment_text ✓, threading ✗, send ✗).

## Open follow-ups already filed as issues

- [#17](https://github.com/hherb/localmail/issues/17) — tighten `tsconfig.json` `noUnusedLocals`/`noUnusedParameters` once real surface lands
- [#18](https://github.com/hherb/localmail/issues/18) — add a CI workflow for `gui/`
- Branded icons + production bundle config → Sub-plan 5

## File map (so you don't have to search)

```
docs/superpowers/specs/2026-05-17-localmail-gui-design.md     # the design spec (all 5 sub-plans)
docs/superpowers/plans/2026-05-17-localmail-gui-client-1-*.md # Sub-plan 1 plan (executed + merged)
docs/superpowers/plans/2026-05-17-localmail-gui-client-2-*.md # Sub-plan 2 plan (executed; in PR)
docs/superpowers/specs/2026-05-17-localmail-gui-design.md     # spec

gui/                                                          # Tauri + Svelte client (separate from python src/)
  src-tauri/src/
    lib.rs            # tauri::Builder::default() with panic hook + invoke_handler registration
    http/             # verifier (TOFU), client (reqwest helpers), errors
    storage/keyring.rs # KeyringStore + KeyringBackend trait + MemKeyring fake
    commands/         # connect, auth, capabilities — thin wrappers calling pure helpers
  src/
    App.svelte        # just <Router />
    routes/Router.svelte
    screens/{ConnectScreen,LoginScreen,AuthenticatedShell}.svelte
    lib/
      tauri.ts        # typed invoke() wrappers
      stores/auth.svelte.ts  # auth state machine (singleton class with $state)
```

Good luck. The hardest part (TLS + TOFU + cross-FFI panics) is done.
