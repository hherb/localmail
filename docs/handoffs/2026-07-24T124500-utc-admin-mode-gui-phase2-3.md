# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-07-24 UTC (session 6).** Shipped **admin-mode GUI phases
> 2 + 3** on branch `feat/admin-mode-gui-phase2-3` (8 commits, `2c97298` →
> `00d80b1`). The desktop app now reveals an **Admin** overlay for `is_admin`
> users with a working **Accounts panel** (CRUD, pause/resume sync, IMAP
> password storage, test-connection). **Branch is NOT pushed and has no PR
> yet** — that is step 0 below.
>
> **Note on the previous handoff:** the NEXT_SESSION.md this session inherited
> was stale — it described session 3 (PR #201). Two sessions ran after it
> (**PR #202** `a580449`, **PR #203** `0b1a98b`) without leaving one. Both of
> its open items were already done: Dependabot open alerts = **0**, and its §1
> docling `max_num_pages` mypy artifact was fixed **at the root** by #202 (it
> was a genuine silent no-op, not just a type complaint).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision, non-blocking lifecycle + admin panel. Web admin UI (HTMX):
account CRUD, user management, archive imports, daemon control. Hybrid search
(Phases 1+2) + an HTTPS GUI server + a remote MCP server + the opt-in
`--smart` LLM query rewriter are all shipped. The MCP server can act as an
**OAuth 2.1 authorization server** (opt-in) with sliding refresh-token
rotation, family revocation on reuse, access-token family containment, and RFC
8707 resource-indicator validation. A Tauri 2 + Svelte 5 GUI lives under
`gui/` — read-only viewer **plus, as of this session, an admin mode**.
Licensed AGPL-3.0-or-later (per-file SPDX headers in `src/localmail/`; **not**
in `gui/`). See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session

### Admin-mode Tauri GUI — phases 2 + 3 (branch `feat/admin-mode-gui-phase2-3`)

Continues the arc from PR #203 (phase 1 = backend bearer auth for
`/v1/admin/*`). Design:
[docs/superpowers/specs/2026-07-23-admin-mode-tauri-gui-design.md](docs/superpowers/specs/2026-07-23-admin-mode-tauri-gui-design.md).
Plan written this session:
[docs/superpowers/plans/2026-07-24-admin-mode-gui-phase2-3.md](docs/superpowers/plans/2026-07-24-admin-mode-gui-phase2-3.md)
(11 TDD tasks; every task watched fail before implementing).

**No Python changed** — the entire surface rides the existing
`/v1/admin/accounts*` JSON API. Confirmed by the Python suite landing on the
identical pre-branch count (1749).

| SHA | what |
|---|---|
| `2c97298` | admin-mode shell — `is_admin` detection + tabbed `AdminView` overlay |
| `12e9aa5` | `http_patch_json` + `http_delete` verb helpers |
| `1a4198e` | admin account list/get Tauri commands |
| `8cd32e1` | admin account create/update/delete/password/test-connection |
| `86b9fb7` | admin accounts TS API wrapper + pure http-status helper |
| `d6bcd86` | `AccountsPanel` + `AccountForm` (list, sync toggle, CRUD) |
| `adae918` | per-account credential storage + IMAP test-connection |
| `00d80b1` | mount Accounts panel in `AdminView`; docs (CLAUDE.md + README.md) |

**New files:** `gui/src-tauri/src/commands/admin/{mod,accounts,accounts_tests}.rs`;
`gui/src/screens/AdminView.svelte`; `gui/src/components/admin/{AccountsPanel,AccountForm,AccountSecrets}.svelte`;
`gui/src/lib/api/admin_accounts.ts`; `gui/src/lib/{admin_error,admin_auth_method}.ts` — each with a test sibling.

**Three decisions worth remembering:**

1. **The PATCH body omits unset fields — load-bearing, not style.**
   `api.admin.accounts.update_account` writes *every key present* in `fields`,
   so a serialized `"imap_host": null` **blanks the column**. Every
   `AdminAccountPatch` field is `#[serde(skip_serializing_if = "Option::is_none")]`,
   pinned by `patch_update_omits_unset_fields_entirely`. `AccountForm` mirrors
   it on the TS side (diffs against the loaded row). Consequence: a cleared
   IMAP port cannot be sent — switch the account to `archive` instead.
2. **`is_admin` decodes with `#[serde(default)]`** so a `serve` predating #203
   still logs in (falls back to `false`) rather than failing to decode.
3. **Pure modules over in-component logic** (project convention):
   `lib/admin_error.ts` (`httpStatusOf`/`isConflict`/`isForbidden`, a
   depth-bounded walk of the nested `{kind, detail}` Rust error shape) and
   `lib/admin_auth_method.ts` (`hasImapEndpoint`/`usesStoredPassword`). The
   latter also fixes a real `svelte-check` error: TS narrows a local `$state`
   to its initialiser's literal type, making `authMethod !== "archive"` look
   unreachable. Routing through a function stops the narrowing.

**Verification (all run this session, all green):**
- `cd gui && npm run check` → **315 files, 0 errors, 0 warnings**
- `cd gui && npm test` → **360 passed** (43 files; was 316/36 pre-branch)
- `cd gui && npm run build` → succeeds
- `cd gui/src-tauri && cargo test` → **95 passed** (was 79 pre-branch)
- `uv run --extra mcp --extra extraction pytest -q tests/ --deselect tests/test_daemon_control_socket.py`
  → **1749 passed**, 14 deselected — *identical to the pre-branch baseline*,
  proving no Python was touched.

## What's next

### 0. **Push the branch and open the PR** *(nothing is on `origin` yet)*
   **Acceptance:** PR open against `main`, CI green (`python-ci` + the
   path-filtered `gui-ci`, which *will* run — this branch touches `gui/**`).
```bash
git push -u origin feat/admin-mode-gui-phase2-3
gh pr create --fill   # or write a body from the commit list above
```

### 1. **Admin GUI phase 4 — Daemon panel** *(the design's next slice)*
   `GET /v1/admin/daemon` fuses supervisor state + heartbeats + recent log;
   `POST /v1/admin/daemon/{start,stop,restart,reload}` and
   `POST /v1/admin/accounts/{id}/restart-sync`. All five are already on a
   router #203 swapped to `require_admin()`, so **no backend work is needed**.
   **Acceptance:** a `DaemonPanel.svelte` replacing the Daemon tab placeholder
   that (a) shows status + heartbeats + recent log lines, (b) marks the status
   red past `heartbeat_stale_seconds` using the **server's** `stale` flag (never
   a client clock), (c) **disables** start/stop/restart when
   `supervise_daemon_externally` is true — which it is under the user's launchd
   deployment — while keeping reload + per-account restart-sync enabled, and
   (d) surfaces a 409 (busy-guard / external stub) as a visible message, not an
   inert button. Mirror the web panel
   ([serve/admin/daemon_panel_router.py](src/localmail/serve/admin/daemon_panel_router.py))
   for exact semantics.

### 2. **Phases 5 + 6 — Users & ACL panel, Imports panel**
   Same shape; both routers are already bearer-capable. Users needs the two
   lock-out guards surfaced as 409s (last-admin, self-demote/self-delete).

### 3. **Gmail "Connect" (OAuth) — BLOCKED on backend work, do not start in the GUI**
   Two independent gaps, both server-side:
   - `POST /v1/admin/accounts/{id}/oauth/start` lives in `oauth_router.py`,
     which #203 did **not** swap to `require_admin()` — it is still
     `require_admin_session()` (cookie-only), so a bearer client cannot start
     the flow.
   - The design's completion check ("poll the account's secret status until the
     refresh token appears") **has no backing field**: `_account_dict` exposes
     no secret status and no `/v1/admin` endpoint reports one.
   **Acceptance for unblocking:** `oauth/start` accepts bearer, *and* some
   endpoint reports per-account secret presence. Note the callback is
   inherently browser/cookie-bound (Google redirects to it), which is fine —
   only `oauth/start` and the poll need to work for a native client.
   `clear_secret` likewise has a service function but no JSON route.

### 4. **(Carried, unrelated) `cargo clippy --all-targets -- -D warnings` fails on `main`**
   `gui/src-tauri/src/commands/search.rs:189` uses `3.14` as a dummy `took_ms`
   in a test → `clippy::approx_constant`. **Pre-existing**, verified not on this
   branch's diff, and CI gates no clippy step. One-character fix (e.g. `3.5`)
   whenever someone is in that file.

## Open decisions & risks
1. **Branch unpushed, no PR.** All 8 commits are local only. §0 above.
2. **`gui-ci` has not run on this work.** Only local checks so far. The branch
   touches `gui/**`, so `.github/workflows/gui-ci.yml` will run for the first
   time on push — expect it to exercise `npm ci && npm run check && npm test &&
   npm run build` plus `cargo check`. Local equivalents are all green.
3. **Admin bearer blast radius** *(carried, tracked as #204)*: a token issued to
   an `is_admin` user is now an admin credential — no per-token scope, and admin
   mutations are not audit-differentiated by auth channel. Deliberate (mirrors
   the session cookie's authority); mitigation unchanged (token lives only in
   the OS keyring).
4. **Three tabs are placeholders.** Daemon / Users / Imports render honest "not
   available in this build yet" text. Operators use the web admin at `/admin/*`
   for those — worth saying out loud if anyone demos the app.
5. **macOS test noise** *(carried)* — `test_daemon_control_socket.py` fails
   locally on macOS (`AF_UNIX path too long`); deselect locally, Linux CI is the
   real signal. Also carried: psycopg_pool teardown `ResourceWarning`s, the
   websockets `DeprecationWarning` (issue #25), the Starlette TestClient `httpx`
   `DeprecationWarning`, and jsdom `HTMLCanvasElement.getContext` noise in the
   gui vitest run.
6. **No ROADMAP.md** *(carried)* — the `/nextsession` ROADMAP step is a no-op;
   slice status lives in NEXT_SESSION/handoffs + the specs. README **was**
   updated this session (admin mode is user-facing).
7. **Run vitest from `gui/`, not the repo root.** `npx vitest` from the root
   silently runs without gui's vite config (no svelte plugin) and fails every
   `.svelte` import with a confusing "invalid JS syntax" parse error. It also
   drops an empty `node_modules/.vite` at the repo root, which is **not**
   gitignored there. Bit me once this session.
8. **`.claude/` local files** stay untracked (incl. `.claude/scheduled_tasks.lock`).
   The SDD progress ledger lives at `.superpowers/sdd/progress.md` (git-ignored).

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # expect clean + untracked .claude lock
git branch --show-current                # feat/admin-mode-gui-phase2-3
git --no-pager log --oneline -8          # HEAD = 00d80b1
gh pr list --state open                  # expect none until §0 is done

# §0 — push + PR:
git push -u origin feat/admin-mode-gui-phase2-3 && gh pr create --fill

# Frontend (MUST be run from gui/ — see risk #7):
cd gui && npm run check && npm test && npm run build && cd ..
#   expect: 0 errors / 360 passed / build ok

# Rust:
cd gui/src-tauri && cargo test && cd ../..
#   expect: 95 passed
#   (cargo clippy -D warnings fails on a PRE-EXISTING search.rs lint — see §4)

# Python (unchanged by this branch; use --extra mcp so MCP/OAuth tests run):
unset VIRTUAL_ENV && uv run --extra mcp --extra extraction pytest -q tests/ \
  --deselect tests/test_daemon_control_socket.py       # expect 1749 passed
unset VIRTUAL_ENV && uv sync --frozen --extra mcp      # restore CI-matching env
unset VIRTUAL_ENV && uv run mypy src/localmail         # expect clean, 122 files
```

`origin/main` at `0b1a98b`; local branch `feat/admin-mode-gui-phase2-3` at
`00d80b1`, **8 commits ahead, unpushed**. Latest migration
`0031_oauth_resource_indicator.sql`; next free slot `0032_*.sql`.
Dependabot open alerts: **0**.
