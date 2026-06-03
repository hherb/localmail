# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-03T1029-utc (vitest security bump).**
> Cleared the **2 critical Dependabot alerts** (#13, #14) for
> [GHSA-5xrq-8626-4rwp](https://github.com/vitest-dev/vitest/security/advisories/GHSA-5xrq-8626-4rwp)
> — *"When Vitest UI server is listening, arbitrary file can be read and
> executed"* (vulnerable range `vitest < 4.1.0`). Bumped the Tauri GUI's
> dev-only `vitest` devDependency `^3.2.4` → `^4.1.0` (resolves to **4.1.8**)
> and regenerated `gui/package-lock.json`. `npm audit` now reports **0
> vulnerabilities**.
>
> Work is on branch **`deps-vitest-4-security`** (branched from `main` at
> `3b9967b`, which merged #152 / closed #47), **pushed**, opened as **PR #154**
> (<https://github.com/hherb/localmail/pull/154>, **open**). It clears the
> vitest entries of the default-branch critical alerts on merge.
>
> **Also done at session start:** confirmed last session's PR #152 (#47 fix) is
> already merged into `main` (commit `3b9967b`); deleted the stale local branch
> `fix-47-extract-transient-thirdparty` (its remote was already gone).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. The daemon hot-reloads its account set (2B.1), records per-thread
heartbeats (2B.2), consumes a DB command queue with LISTEN/NOTIFY wake (2B.3),
is supervised + controllable via two planes (2B.4), and has non-blocking
lifecycle control + an admin panel (2B.5). **The 2B arc is complete.** A Tauri +
Svelte GUI lives under `gui/` (tracked; dev test stack = vitest + svelte-check).
Downstream consumers read the DB + attachment tree directly or via the
`localmail serve` HTTPS API. See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we did this session (vitest security bump)

### The change

- **`gui/package.json`**: `vitest` `^3.2.4` → `^4.1.0`.
- **`gui/package-lock.json`**: regenerated via `npm install` (added 3, removed
  10, changed 13 packages). vitest resolves to **4.1.8** throughout the tree;
  no 3.x vitest remains.
- **No companion peer bumps needed**: `@testing-library/svelte@5.3.1` declares
  `vitest: '*'` as its peer dependency, so the 3→4 major bump is clean.
- **No `vitest.config` changes**: config lives in
  [gui/vite.config.ts](gui/vite.config.ts) as just `test: { environment:
  "jsdom", globals: true }` — no removed-in-v4 APIs in play.

### Why this is low-risk

- `vitest` is a **dev-only** `devDependency` — it never ships in the Tauri
  binary or the served app. The advisory is about the Vitest *UI dev server*,
  not anything in production.

### Docs

- No README/ROADMAP changes: root [README.md](README.md) never mentions vitest;
  [gui/README.md](gui/README.md) names vitest as the test runner but pins no
  version; **there is no ROADMAP.md** in this repo (the `/nextsession` ROADMAP
  step is a no-op here, as in prior handoffs).

### Commit on `deps-vitest-4-security`

```
84f3093  chore(deps): bump vitest 3.2.4 → ^4.1.0 to clear GHSA-5xrq-8626-4rwp (#13, #14)
```

### Verification (this session)

Run from `gui/` (this change touches only the JS frontend; the Python suite is
unaffected):

- `npm run check` (svelte-check) — **0 errors, 0 warnings** (301 files).
- `npm test` (vitest run) — **312 passed / 36 files** (identical to the vitest-3
  baseline captured before the bump).
- `npm audit` — **found 0 vulnerabilities**.
- The `HTMLCanvasElement.getContext` stderr lines in `npm test` output are
  **pre-existing jsdom limitations** (jsdom ships no canvas) from the PDF
  preview component (`AttachmentPreviewModal.svelte`) — version-independent,
  not test failures.

The Python suite was confirmed green at session start (unchanged by this work):
`unset VIRTUAL_ENV && uv run pytest -q tests/` → **1233 passed**.

## What's next

### 0. **Review & merge PR #154** *(immediate)*

PR #154 (<https://github.com/hherb/localmail/pull/154>) is **open**; gui-ci
(svelte-check + vitest) runs on the `gui/**` path filter. After it's green and
merged, the 2 critical default-branch Dependabot alerts clear:

```bash
gh pr merge 154 --squash --delete-branch
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D deps-vitest-4-security
# confirm alerts cleared:
gh api repos/hherb/localmail/dependabot/alerts --jq '.[] | select(.state=="open") | {number,severity,pkg:.dependency.package.name}'
```

### 1. **Pick the next issue or the next feature**

**Open issues (after #154 merges, vitest alerts close): 4** — #153, #125, #90, #25.

- **#153** (cap transient docling re-attempts, **filed last session**) — the #47
  fix routes third-party docling network errors (`huggingface_hub`, `requests`,
  …) through the transient path, which **never increments `retry_count`**, so a
  *permanently* failing network error (HF 401/403 from a bad token, 404 for a
  removed model) re-attempts **every sweep forever** instead of capping at
  `extract_worker_max_retries`. Fix needs retry state **independent of
  `retry_count`** (the transient path deliberately doesn't touch it). Likely a
  small migration for a new counter/column. Bounded to docling-eligible PDFs;
  surfaces as repeated WARNINGs today. Good self-contained TDD issue.
- **#90** (glib/Tauri Dependabot, medium) — Rust/Cargo Tauri-stack dependency
  walk (`cargo tree -i glib`, bump `tauri`/`tauri-plugin-*` or
  `[patch.crates-io]`). May be blocked on upstream tauri versions.
- **#25** (websockets depwarn) — *not actionable* until uvicorn ships an
  upstream release on the new `websockets.asyncio` API; only a `filterwarnings`
  band-aid is possible now.
- **#125** (accounts HTML must mint method-bound CSRF) — **stays open until
  2A.3** adopts `csrf_token_for_method` for the account screens (its subject).

### 2. **Sub-plan 2A.3 — account CRUD admin screens** *(next real feature)*

The 2B arc is done; the remaining open admin-UI work is account-management
screens. Service layer already exists
([src/localmail/api/admin/accounts.py](src/localmail/api/admin/accounts.py):
`list_accounts`, `get_account`, `create_account`, `update_account`,
`delete_account`, `store_password`, `clear_secret`, `probe_connection`) and the
web OAuth flow ([api/admin/oauth.py](src/localmail/api/admin/oauth.py)). 2A.3 is
the HTML UI on top.
- **Reuse, don't reinvent:** mint method-bound CSRF via
  `csrf_token_context().csrf_token_for_method` (closes #125's intent); follow
  the daemon panel's HTMX self-poll / per-button `hx-headers` pattern.
- **CSP gotcha (proven by #148):** any panel JS must be a **served static
  file** (`script-src 'self'`), not inline and not an htmx `hx-on::` handler.
- **Acceptance:** list/create/edit/delete account screens; password + OAuth
  flows wired to the existing service layer + JSON routes; per-control
  method-bound CSRF; TDD; no magic numbers. **No spec/plan yet — brainstorm →
  spec → plan first.**

## Open decisions & risks

1. **vitest 3 → 4 is a major bump but dev-only.** No production surface; the 312
   GUI tests pass unchanged and `@testing-library/svelte` accepts vitest 4 via a
   `'*'` peer range. If a future GUI change reaches for a vitest API, note v4
   removed several v3 deprecations — consult the
   [vitest 4 migration guide](https://vitest.dev/guide/migration) before relying
   on older config shapes.
2. **#153 is the live trade-off introduced by #47.** The transient path (ROLLBACK
   + WARNING, no `failed_extractions` row, `retry_count` untouched) means a
   *permanently* failing third-party network error retries forever. To fix
   without regressing #47, cap transient re-attempts with **state independent of
   `retry_count`** — never widen the builtin `_TRANSIENT_EXC_TYPES`, and never
   demote genuine transients to permanent.
3. **Dependabot after #154** — the 2 critical (vitest) alerts clear on merge.
   The standing **medium #90** (glib via the Tauri Rust stack) remains; triage at
   <https://github.com/hherb/localmail/security/dependabot>.
4. **Migration numbering** — latest applied is **0024** (daemon_commands). This
   session added **no** migration. Next free slot: `0025_*.sql` (likely needed
   by #153 for a transient-retry counter). Re-check `ls migrations/` at
   plan-time.
5. **No ROADMAP.md** in this repo *(carried)* — slice status lives in
   NEXT_SESSION/handoffs + the specs. The `/nextsession` ROADMAP step is a
   no-op here by design.
6. **Heartbeat vocabulary still load-bearing** *(carried)* — any new heartbeat
   call site must use a `worker_kind`/`state` present in both the SQL CHECK
   lists (0023) and the `WorkerKind`/`WorkerState` Literals; all loop
   heartbeats go through `safe_heartbeat`.
7. **Python tooling note** *(carried)* — the full Python suite emits harmless
   psycopg pool `__del__` ResourceWarnings at interpreter teardown — *not* a
   failure (`1233 passed`).
8. **GUI test noise** — `npm test` prints `HTMLCanvasElement.getContext` stderr
   from the PDF preview component (jsdom has no canvas). Pre-existing and
   version-independent; not a failure. If it ever needs silencing, install the
   `canvas` npm package or stub `getContext` in the test setup.
9. **`.claude/` local files** stay untracked, by design.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/ local files
git branch -vv                           # main + deps-vitest-4-security (pushed, PR #154)
git --no-pager log --oneline -8
gh issue list --state open --limit 40    # 4 open; vitest alerts close when #154 merges

# Verify the GUI bump (from gui/):
cd gui
npm ci                                   # CI uses this; lockfile must be in sync
npm run check                            # expect 0 errors
npm test                                 # expect 312 passed / 36 files
npm audit                                # expect 0 vulnerabilities
cd ..

# Python suite unaffected this session, but to confirm:
unset VIRTUAL_ENV && uv run pytest -q tests/   # expect 1233 passed
unset VIRTUAL_ENV && uv run mypy src/localmail # expect clean, 84 files
```

After PR #154 merges, pick the next issue — **#153 (cap transient docling
retries)** is the most self-contained — or start **2A.3 (account CRUD admin
screens)**:

```bash
git checkout main && git pull
git checkout -b fix-153-cap-transient-retries   # or  admin-ui-2a3-account-screens
ls migrations/    # latest is 0024; #153 likely needs 0025_*.sql
# for 2A.3: brainstorm → spec → plan first (routes exist; screen design does not)
```

## File map (this session)

```
NEXT_SESSION.md                                          # REPLACED this session
gui/package.json                                         # vitest ^3.2.4 → ^4.1.0
gui/package-lock.json                                    # regenerated (vitest 4.1.8; npm audit clean)
docs/handoffs/2026-06-03T1029-utc-vitest-4-security.md   # frozen snapshot of this file
```

`main` at `3b9967b` (== `origin/main`, merged #152, closed #47). Branch
`deps-vitest-4-security` **pushed** (== its `origin/` ref), **PR #154 open**
(clears critical Dependabot alerts #13/#14 on merge). Working tree clean (only
`.claude/` local files). 2 local branches (`main`, `deps-vitest-4-security`); 1
open PR (#154). **No migration changed this session.**
