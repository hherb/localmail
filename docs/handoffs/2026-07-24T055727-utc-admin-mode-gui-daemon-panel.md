# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-07-24 UTC (session 7).** Shipped **admin-mode GUI phase
> 4 — the Daemon panel** on branch `feat/admin-mode-gui-daemon-panel`
> (1 feature commit `13e4afb` + this handoff commit), **not yet pushed / no PR
> yet**. The desktop app's Admin overlay now has a working **Daemon** tab:
> self-refreshing status + heartbeats + recent log, lifecycle controls (gated
> on external supervision), reload, and per-account restart-sync.
> **Next step: push + open PR — see §0.**
>
> **Note on the previous handoff:** session 6's NEXT_SESSION.md said "merge PR
> #205" (§0). That is **done** — PR #205 merged as `bea1eb6`, branch deleted.
> This session started from a clean `main` and built the next slice (§1 of the
> old handoff, "Admin GUI phase 4 — Daemon panel").

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision, non-blocking lifecycle + admin panel. Web admin UI (HTMX):
account CRUD, user management, archive imports, daemon control. Hybrid search
(Phases 1+2) + an HTTPS GUI server + a remote MCP server + the opt-in
`--smart` LLM query rewriter are all shipped. The MCP server can act as an
**OAuth 2.1 authorization server** (opt-in). A Tauri 2 + Svelte 5 GUI lives
under `gui/` — read-only viewer **plus an admin mode** (Accounts + Daemon
panels shipped; Users + Imports still placeholders). Licensed
AGPL-3.0-or-later (per-file SPDX headers in `src/localmail/`; **not** in
`gui/`). See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session

### Admin-mode Tauri GUI — phase 4, the Daemon panel (branch `feat/admin-mode-gui-daemon-panel`)

Continues the arc from PR #203 (phase 1 backend bearer auth) and PR #205
(phases 2+3 shell + Accounts panel). Design:
[docs/superpowers/specs/2026-07-23-admin-mode-tauri-gui-design.md](docs/superpowers/specs/2026-07-23-admin-mode-tauri-gui-design.md)
("DaemonPanel" bullet + "Daemon lifecycle under launchd" flow). Built TDD
throughout — every layer's test watched fail before implementing.

**No Python changed** — the whole surface rides the existing
`/v1/admin/daemon*` + `/v1/admin/accounts/{id}/restart-sync` JSON API, all
`require_admin()` (bearer-capable; CSRF skipped for bearer). Confirmed: the
Python suite is untouched (zero `.py` files in the diff).

| SHA | what |
|---|---|
| `13e4afb` | **feat(gui): admin-mode Daemon panel (phase 4)** — Rust proxies + TS wrapper + pure dedup helper + DaemonPanel.svelte + AdminView wiring + docs |

**New files** (each with a test sibling): `gui/src-tauri/src/commands/admin/{daemon,daemon_tests}.rs`;
`gui/src/lib/api/admin_daemon.ts`; `gui/src/lib/daemon_view.ts`;
`gui/src/components/admin/DaemonPanel.svelte`.
**Modified:** `commands/admin/mod.rs`, `lib.rs` (register 4 commands),
`screens/AdminView.svelte` (Daemon tab → `<DaemonPanel />`),
`AdminView.test.ts` + `MainView.test.ts` (stub `admin_daemon`), `README.md`,
`CLAUDE.md`.

**Four decisions worth remembering:**

1. **Staleness is the server's `hb.stale` flag alone, never a client clock.**
   `GET /v1/admin/daemon` (fused by `daemon_router.build_daemon_view`) computes
   `stale` per heartbeat against `heartbeat_stale_seconds`. The panel renders
   `class:daemon-stale={hb.stale}` — no `Date.now()` anywhere. Mirrors the web
   panel + #148.
2. **Lifecycle vs Plane-A split.** start/stop/restart are
   `disabled={busy || view.supervise_daemon_externally}`; reload + per-account
   restart-sync are only `disabled={busy}`. Under the user's launchd
   deployment `supervise_daemon = false`, so `supervise_daemon_externally` is
   true and the lifecycle buttons are correctly inert while the DB-mediated
   controls still work. A busy-guard **409** (`isConflict`) surfaces as a
   visible `daemon-action-message`, not an inert button.
3. **Poll-interval unmount leak (found in code review, fixed TDD).** `onMount`
   awaits the first `getAdminDaemon()` before assigning `pollTimer`; an unmount
   *during* that fetch ran `onDestroy` (pollTimer still null → cleared nothing)
   and then started a `setInterval` onto the dead component that polled
   forever. Fixed with a `destroyed` flag checked after the await. Pinned by
   `DaemonPanel.test.ts::"does not keep polling after unmount during the
   initial fetch"` (fake timers; reproduces the 4-calls-instead-of-1 leak).
4. **The unhandled-rejection CI trap (carried from #205).** Any admin panel
   that fetches on mount MUST be stubbed in **both** `AdminView.test.ts` and
   `MainView.test.ts` (both mount the overlay), or vitest leaks an unhandled
   rejection while still printing "passed". Both are stubbed here.

**Verification (all run this session, all green):**
- `cd gui && npm run check` → **321 files, 0 errors, 0 warnings**
- `cd gui && npm test` → **386 passed** (46 files; was 361/43 on `main`), no
  "Unhandled Errors" block (only the carried jsdom `getContext` noise).
- `cd gui && npm run build` → succeeds
- `cd gui/src-tauri && cargo test` → **103 passed** (was 95)
- `cargo clippy --locked -- -D warnings` → clean (exact CI invocation)

## What's next

### 0. **Push + open PR** — CI has not run yet
   Branch has the feature commit + this handoff commit. Not pushed.
```bash
git push -u origin feat/admin-mode-gui-daemon-panel && gh pr create --fill
gh pr checks --watch          # then squash-merge once green
```
   CI (`gui-ci.yml`) runs `npm run check && npm test && npm run build` +
   `cargo test` + `cargo clippy --locked -- -D warnings` on ubuntu + macos.
   All pass locally. Watch the vitest job for a green run (the #205 unhandled-
   rejection trap is guarded here, but confirm).

### 1. **Admin GUI phase 5 — Users & ACL panel** *(the design's next slice)*
   The `/v1/admin/users` JSON router is **already `require_admin()`
   (bearer-capable)** — verified this session — so **no backend work needed**.
   Service layer: [src/localmail/api/admin/users.py](src/localmail/api/admin/users.py).
   **Acceptance:** a `UsersPanel.svelte` replacing the Users tab placeholder
   that lists users and offers create / delete, per-account ACL grant/revoke
   (a checklist over every account), `is_admin` toggle, password reset, and
   enable/disable (`disabled_at`). Surface the **two lock-out guards as 409s**:
   the count-based last-admin rule (`LastAdminError`) and the identity-based
   self-action rule (no self-demote / self-delete). Mirror the web panel
   ([serve/admin/users_panel_router.py](src/localmail/serve/admin/users_panel_router.py)
   + [user_forms.py](src/localmail/serve/admin/user_forms.py)) for exact
   semantics. Follow the daemon-panel shape: Rust proxies in
   `commands/admin/users.rs` (+ split tests), TS wrapper `lib/api/admin_users.ts`,
   pure logic in `lib/`, `UsersPanel.svelte`, and **stub the new API module in
   both `AdminView.test.ts` and `MainView.test.ts`**.

### 2. **Admin GUI phase 6 — Imports panel**
   Same shape; `/v1/admin/imports` is `require_admin()` (bearer-capable).
   Service: [src/localmail/api/admin/imports.py](src/localmail/api/admin/imports.py).
   **Acceptance:** create / list / cancel mbox & maildir jobs with progress
   (inserted/skipped/failed counts, stale detection past `[imports].stale_seconds`).
   Mirror [serve/admin/imports_panel_router.py](src/localmail/serve/admin/imports_panel_router.py).

### 3. **Gmail "Connect" (OAuth) — STILL BLOCKED on backend work, do not start in the GUI**
   Unchanged from session 6. Verified again this session: `oauth_router.py` is
   the **only** `/v1/admin/*` router still on `require_admin_session()`
   (cookie-only) — every other one (`accounts`, `users`, `imports`, `daemon`)
   is bearer-capable. Two independent server-side gaps:
   - `POST /v1/admin/accounts/{id}/oauth/start` cannot be called by a bearer
     client until `oauth_router` is swapped to `require_admin()`.
   - The design's completion check ("poll secret status until the refresh token
     appears") has **no backing field** — `_account_dict` exposes no secret
     status and no `/v1/admin` endpoint reports one.
   **Acceptance for unblocking:** `oauth/start` accepts bearer, *and* some
   endpoint reports per-account secret presence. `clear_secret` also has a
   service function but no JSON route. The callback is inherently
   browser/cookie-bound (Google redirects to it), which is fine.

### 4. **(Carried, unrelated) `cargo clippy --all-targets -- -D warnings` fails on `main`**
   `gui/src-tauri/src/commands/search.rs:189` uses `3.14` as a dummy `took_ms`
   in a test → `clippy::approx_constant`. **Pre-existing**, verified again this
   session that it is NOT on this branch's diff (my new `daemon_tests.rs` is
   clippy-clean under `--all-targets`). CI gates clippy **without**
   `--all-targets`, so `#[cfg(test)]` modules are never linted → `main` stays
   green. One-character fix (e.g. `3.5`) whenever someone is in that file;
   adding `--all-targets` to CI would require fixing it first.

## Open decisions & risks
1. **Branch not pushed, no PR, CI unrun.** §0 above. Local checks all green.
2. **`gui-ci` clippy runs WITHOUT `--all-targets`** — so the pre-existing
   `search.rs:189` test-lint (§4) does not fail CI, and neither would a future
   test-only lint. Worth adding `--all-targets` someday (after fixing §4).
3. **Admin bearer blast radius** *(carried, tracked as #204)*: a token issued to
   an `is_admin` user is now an admin credential — no per-token scope. The
   daemon lifecycle/reload/restart-sync controls inherit this. Deliberate
   (mirrors the session cookie's authority); token lives only in the OS keyring.
4. **Two tabs are placeholders.** Users / Imports render honest "not available
   in this build yet" text; operators use the web admin at `/admin/*`. Say this
   out loud if anyone demos the app.
5. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
   locally on macOS (`AF_UNIX path too long`); deselect locally, Linux CI is the
   real signal. Also carried: psycopg_pool teardown `ResourceWarning`s, the
   websockets `DeprecationWarning` (#25), Starlette TestClient `httpx`
   `DeprecationWarning`, and — in the gui vitest run — jsdom
   `HTMLCanvasElement.getContext` noise from `AttachmentPreviewModal` (grep past
   it; it is NOT an unhandled-error block).
6. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
   slice status lives in NEXT_SESSION/handoffs + the specs. README **was**
   updated this session (Daemon panel is user-facing).
7. **Run vitest from `gui/`, not the repo root** *(carried)* — `npx vitest` from
   the root silently runs without gui's vite config (no svelte plugin) and fails
   every `.svelte` import with a confusing "invalid JS syntax" parse error, and
   drops an ungitignored `node_modules/.vite` at the repo root.
8. **`.claude/` local files** stay untracked (incl. `.claude/scheduled_tasks.lock`).

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # expect clean + untracked .claude lock
git branch --show-current                # feat/admin-mode-gui-daemon-panel
git --no-pager log --oneline -5          # HEAD = the handoff commit; feature = 13e4afb
gh pr list --state open                  # expect none until §0 is done

# §0 — push + PR:
git push -u origin feat/admin-mode-gui-daemon-panel && gh pr create --fill

# Frontend (MUST be run from gui/ — see risk #7):
cd gui && npm run check && npm test && npm run build && cd ..
#   expect: 0 errors / 386 passed / build ok / NO "Unhandled Errors" block

# Rust (exact CI invocation):
cd gui/src-tauri && cargo test && cargo clippy --locked -- -D warnings && cd ../..
#   expect: 103 passed, clippy clean
#   NB: adding --all-targets surfaces the PRE-EXISTING search.rs lint — see §4

# Python (unchanged by this branch; nothing to run, but to prove it):
git --no-pager diff --stat main -- '*.py'   # expect: no output (zero .py changed)
```

`origin/main` at `bea1eb6`; branch `feat/admin-mode-gui-daemon-panel` = feature
`13e4afb` + this handoff commit, **not yet pushed**. Latest migration
`0031_oauth_resource_indicator.sql`; next free slot `0032_*.sql`. Dependabot
open alerts: **0**.
