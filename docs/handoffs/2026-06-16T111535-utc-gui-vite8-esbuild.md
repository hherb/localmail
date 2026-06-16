# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-16 (gui vite 6→8 / esbuild alert).**
> Last session's PR **#187** (pluggable `--smart` rewriter backends) is **merged**
> (`fa92c67`), and a Dependabot pypdf bump landed as **#188** (`9017ef8`, current
> `origin/main` HEAD). This session cleared **Dependabot alert #20 (esbuild,
> high)** by bumping the `gui/` toolchain — shipped as **PR #190** (open, CI
> green, branch `chore/gui-vite8-esbuild`). esbuild is now fully absent from the
> frontend tree; `npm audit` reports 0 vulnerabilities. gui suite: **312 passed
> (36 files)**, `svelte-check` 0 errors, `vite build` OK.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The database is canonical for accounts.
Daemon: hot-reload account set, heartbeats, DB command queue, two-plane
supervision, non-blocking lifecycle + admin panel. Admin UI: account CRUD, user
management, archive imports, daemon control. Hybrid search (Phases 1+2) + an
HTTPS GUI server + a remote MCP server + the opt-in `--smart` LLM query rewriter
(Ollama / OpenAI-compat / Anthropic backends) are all shipped. The MCP server
can act as an **OAuth 2.1 authorization server** (opt-in) with sliding
refresh-token rotation + family revocation on reuse. A Tauri 2 + Svelte 5 GUI
lives under `gui/`. See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session

### gui vite 6→8 + vite-plugin-svelte 5→7 — clears esbuild alert #20 (PR #190, CI green)

Dependabot alert **#20 — esbuild (high)** (GHSA RCE via `NPM_CONFIG_REGISTRY`
in esbuild's **Deno** module; the vector is *not* exercised by our npm/vite
build) couldn't be cleared on vite 6, whose dep range caps esbuild at `^0.25.x`.
vite 7 only reaches esbuild `^0.27.0`; **vite 8** peers `^0.27.0 || ^0.28.0` and
makes esbuild an *optional* peer — after the bump **nothing pulls esbuild at
all**, so it's fully removed from the tree and the alert clears.

- `vite ^6.4.2 → ^8.0.16` (engine `^20.19 || >=22.12`; CI's node 22 satisfies it).
- `@sveltejs/vite-plugin-svelte ^5.0.1 → ^7.1.2` (peers `vite ^8` + `svelte
  ^5.46.4`; installed `svelte` already `5.56.3`, so no svelte bump needed).
- `vitest` stays `^4.1.0` (already peers `vite ^8`); `gui/package-lock.json`
  regenerated so `npm ci` stays in sync.
- Dropped the `vitePreprocess({ style: false })` workaround in
  `gui/svelte.config.js` — it guarded a **vite-6-specific** vitest
  `preprocessCSS` crash and its own comment invited reverting once upstream
  fixed it. Plain `vitePreprocess()` now passes test + check + build on vite 8.

Only `gui/` changed (3 tracked files: `package.json`, `package-lock.json`,
`svelte.config.js`). No Python, no migration, no CI-workflow change. README /
gui/README only mention "Tauri 2 + Svelte 5" generically (no version pins) — no
doc edit needed. No `ROADMAP.md` in this repo.

Commits on `chore/gui-vite8-esbuild` (pushed; PR #190):

| SHA | what |
|---|---|
| `a0a8d7d` | chore(gui): bump vite 6→8 + vite-plugin-svelte 5→7 to clear esbuild alert (#189) |

**Verification:** in `gui/` — `npm test` → 312 passed (36 files); `npm run
check` → 0 errors; `npm run build` → OK (vite v8.0.16); no `esbuild` in the
resolved tree; `npm audit` → 0 vulnerabilities. CI (PR #190): all 3 jobs green
(svelte-check + vitest on node 22; cargo test + clippy on ubuntu + macos).

## What's next

### 0. **Merge PR #190** *(immediate)*
   CI is green (`gh pr checks 190` → all pass). Squash-merge, delete branch,
   advance `origin/main`.
   **Acceptance:** PR #190 squash-merged (closes #189); `origin/main` past
   `9017ef8`; **Dependabot alert #20 (esbuild) auto-resolves** once the
   esbuild-free lockfile is on the default branch — confirm at
   https://github.com/hherb/localmail/security/dependabot.
```bash
gh pr checks 190
gh pr merge 190 --squash --delete-branch
git checkout main && git fetch --prune origin && git pull
```

### 1. **(Optional follow-up) Access-token family containment** *(carried from #186)*
   The OAuth refresh-family DELETE revokes refresh tokens only; access tokens
   already minted along the chain live in `api_tokens` with no `family_id`
   correlation, so they stay valid at `/mcp` until their ≤1h TTL. Instant
   containment would need a `family_id` (or `oauth_client_id`-scoped
   correlation) on `api_tokens` + a join in the reuse DELETE — a schema change
   (migration `0030_*.sql`). Needs its own brainstorm → spec → plan; no issue
   filed. **Low priority** (1h bound is standard AS behaviour).

### 2. **Upstream-blocked (not actionable)** — **#90** (glib/Tauri stack bump),
   **#25** (websockets/uvicorn depwarn).

## Open decisions & risks
1. **PR #190 is open, not merged.** First action next session is §0 (merge).
   Once merged, Dependabot alert #20 should clear on its own (the fix removes
   esbuild from the default-branch lockfile) — verify on the security tab.
2. **esbuild is *removed*, not upgraded.** vite 8 made esbuild an optional peer
   and our config uses no esbuild-requiring feature, so the tree has zero
   esbuild. If a future dep (or a vite plugin needing esbuild) reintroduces it,
   npm will resolve it under vite 8's `^0.27 || ^0.28` peer — still alert-clear.
3. **vite 8 raised the node-engine floor** to `^20.19 || >=22.12`. CI uses
   node 22 (fine) and local dev here is node 26 (fine). A contributor on node
   18 / early-20 would now be unsupported by vite — note for the gui README if
   it ever pins a node version (it currently doesn't).
4. **`{ style: false }` workaround removed.** All `<style>` blocks in the gui
   are plain CSS (no `:global`, no PostCSS directives), so default
   `vitePreprocess()` is safe; full suite + build confirm it. If a future
   component adds PostCSS/`:global` and vitest regresses, the history of this
   change documents the prior guard.
5. **GitHub still reports 12 vulnerabilities on the default branch** (push
   warning: 6 high / 1 mod / 5 low) — those are pre-existing alerts *other* than
   #20 (the merge of #190 clears #20 specifically). #90 and #25 are two of them
   (upstream-blocked). Triage the remainder on the security tab when picking up
   dep work.
6. **macOS test noise** *(carried)* — Python `test_daemon_control_socket.py`
   fails locally on macOS (`AF_UNIX path too long`); deselect locally, Linux CI
   is the real signal. Also carried: psycopg_pool teardown `ResourceWarning`s,
   the websockets `DeprecationWarning` (#25), the Starlette TestClient httpx
   `DeprecationWarning`. (None of these are touched by the gui-only #190.)
7. **No ROADMAP.md** in this repo *(carried)* — the `/nextsession` ROADMAP step
   is a no-op; slice status lives in NEXT_SESSION/handoffs + the specs.
8. **`.claude/` local files** stay untracked (incl. `.claude/scheduled_tasks.lock`).

## Exact commands to resume
```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # expect clean (only the untracked .claude lock)
git --no-pager log --oneline -5
gh pr list --state open                  # expect PR #190 (merge it — §0)
gh pr view 190
gh issue list --state open --limit 40    # #189 (closed by #190), #90, #25

# §0 — merge the gui dep-bump PR (CI already green):
gh pr checks 190
gh pr merge 190 --squash --delete-branch
git checkout main && git fetch --prune origin && git pull

# gui frontend checks (run inside gui/):
cd gui && npm ci && npm run check && npm test && npm run build && cd ..

# Python suite + types (use --extra mcp so the MCP/OAuth tests actually run):
unset VIRTUAL_ENV && uv run --extra mcp pytest -q tests/ \
  --deselect tests/test_daemon_control_socket.py            # expect ~1658 passed
unset VIRTUAL_ENV && uv run mypy src/localmail              # expect clean, 121 files
```

`origin/main` at `9017ef8`; feature branch `chore/gui-vite8-esbuild` is PR #190.
Latest migration `0029_oauth_refresh_token_family.sql`; next free slot
`0030_*.sql`.
