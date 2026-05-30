# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-29T2246 UTC.**
> This session shipped **Sub-plan 2A.2b — the daemon now reads accounts from
> the DB** (live + `sync_enabled`), retiring the config-driven column overwrite
> (prior risk #2) and folding in 2A.2c (`sync_enabled`). Opened as **PR #132**
> (`feat(daemon): read accounts from the DB (Sub-plan 2A.2b)`), **open, not yet
> merged**. Branch `sub-plan-2a2b-daemon-db-source` pushed; tip `ca61c07`.
>
> Reconciled remote state at session start: **PR #131 (parser typing-debt fix)
> had already merged** — local `main` is at `cbfa6ec`. Pruned the now-`[gone]`
> `fix-parser-typing-debt` branch.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP**. Downstream consumers read the DB
+ attachment tree directly or via the `localmail serve` HTTPS API. The Tauri
2 + Svelte 5 desktop GUI lives at [gui/](gui/). The HTTPS admin UI ships under
[src/localmail/serve/admin/](src/localmail/serve/admin/) and
[src/localmail/api/admin/](src/localmail/api/admin/); end-to-end design in
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we shipped this session

**Sub-plan 2A.2b**: the `localmail run` daemon stops reading `cfg.accounts`
(TOML) and enumerates the `accounts` DB table instead — making the DB
authoritative for the running daemon. TDD, subagent-driven, two-stage review
per task + a final whole-implementation review (verdict: ready to merge).

Spec: [docs/superpowers/specs/2026-05-29-daemon-db-account-source-design.md](docs/superpowers/specs/2026-05-29-daemon-db-account-source-design.md)
Plan: [docs/superpowers/plans/2026-05-29-daemon-db-account-source.md](docs/superpowers/plans/2026-05-29-daemon-db-account-source.md)

11 commits on `sub-plan-2a2b-daemon-db-source` (PR #132):

```
1d992e1  docs(spec): daemon reads accounts from the DB (Sub-plan 2A.2b)
c2737d3  docs(plan): daemon reads accounts from the DB (Sub-plan 2A.2b)
0577e09  feat(daemon): pure DB-row -> AccountConfig adapter
fac5dd0  feat(admin): list_syncable_accounts — daemon account source
2bb9788  fix(sync): upsert_account is get-or-create, no longer overwrites canonical columns
135d102  feat(daemon): enumerate accounts from the DB; carry account_id on WorkerContext
ecb74eb  refactor(daemon): workers use ctx.account_id instead of upsert_account
8bbef6d  test(daemon): harden no-upsert_account regression guard
b8269ee  docs(claude): daemon reads accounts from the DB; retire risk #2 note
8c259a6  test(daemon): seed DB accounts for daemon-thread tests under DB-canonical model
ca61c07  docs(config): note AccountConfig.poll_seconds is no longer consumed
```

### What the change does (PR #132)

- **Daemon enumerates the DB.** `Daemon.__init__` reads live, `sync_enabled`
  accounts via the new `api.admin.accounts.list_syncable_accounts` (a one-shot
  `psycopg.connect`, *before* the pool opens — pool sizing depends on the
  count). Archive and `sync_enabled = FALSE` accounts spawn no threads
  (2A.2c folded in).
- **Pure adapter.** `daemon_accounts.account_config_from_row(Account) ->
  AccountConfig` maps a DB row to the existing `AccountConfig` worker boundary
  (no IO; raises on archive / missing host — defensive over the query filter).
- **`account_id` on `WorkerContext`.** The daemon carries the DB id, so
  `idle.py:_ensure_inbox_row` and `poller.py:_one_poll_pass` use
  `ctx.account_id` and **no longer call `upsert_account`**.
- **`upsert_account` neutered to get-or-create.** `ON CONFLICT (name) DO
  UPDATE SET name = accounts.name RETURNING id` — never overwrites canonical
  columns. Sole remaining caller is the one-shot `localmail sync` CLI (2A.2d).
- **Behavioral deltas (documented in CLAUDE.md + config.py comment):**
  per-account `poll_seconds` TOML override is no longer honored by the daemon
  (no DB column; daemon-wide `[daemon].poll_seconds` applies);
  `sync_enabled=FALSE` and `archive` accounts get no threads.
- **No new migration** — `0020_accounts_canonical.sql` already carries every
  column. CLI account commands + one-shot `sync` rewiring intentionally left
  for 2A.2d.
- Files: `src/localmail/daemon_accounts.py` (new), `daemon.py`, `worker.py`,
  `api/admin/accounts.py`, `sync.py`, `idle.py`, `poller.py`, `config.py`
  (comment); tests across `test_daemon_accounts.py` (new), `test_admin_accounts.py`,
  `test_sync.py`, `test_daemon.py`, `test_daemon_pool.py`,
  `test_daemon_extract_thread.py`, `test_daemon_embed_thread.py`; CLAUDE.md.

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1012 passed**, 6 warnings
  (pre-existing cosmetic websockets/uvicorn + pool `__del__` teardown noise).
  Was 999 on `main`; **+13 new tests**.
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 72 files**.
- Independently re-ran the full suite + mypy as the final gate (not just the
  subagent's word): confirmed green.

### A note on a transient test failure (investigated, no action needed)

During the first full-suite run, `test_admin_oauth_state.py::test_decode_
rejects_tampered_signature` failed once. **It is NOT flaky and NOT related to
this branch.** The test is pure (fixed key, no DB/shared state). I empirically
ran its logic across 400 time-varying payloads (0 false-negatives) and ran the
full suite again (it passed). `main`'s full suite is also clean. Treat any
single recurrence as an environmental transient (e.g. the *separate*
daemon-thread regression we fixed had leaked a pool/threads on failure); do not
"fix" the oauth_state test.

### Docs

- **NEXT_SESSION.md** — *replaced this session* (this file).
- **docs/handoffs/2026-05-29T2246-utc-post-pr132-daemon-db-source.md** — *new*
  (this file's frozen snapshot).
- **CLAUDE.md** — updated (DB-canonical daemon invariant; risk #2 retired) in
  commit `b8269ee`.
- **README.md** — *not touched*. The user-facing workflow is unchanged (edit
  `config.toml` → `init-db` seeds the DB → daemon runs), README already
  documents "the DB is authoritative", and per-account `poll_seconds` was never
  in README. The new `sync_enabled` honoring has no CLI toggle yet (2A.2d), so
  nothing user-facing to add. Revisit when 2A.2d lands.
- **ROADMAP.md** — does not exist in this repo. Not created.

## What's next

### 0. **Merge PR #132** *(immediate)*

PR #132 is open and green (1012 tests, mypy clean, final review = ready to
merge). Review + merge, then `git fetch --prune`, fast-forward local `main`,
prune `sub-plan-2a2b-daemon-db-source`.

### 1. **Sub-plan 2A.2d — rewire CLI account commands to the DB** *(next code work)*

From [docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md) § 5. Now that the daemon is DB-canonical, the CLI is the last TOML-coupled surface.

- Point `add-account` / `oauth-login` / `remove-account` / `list-accounts` and
  the **one-shot `localmail sync`** at `api.admin.accounts` (DB) instead of
  `config.toml`. Medium risk (operator UX).
  - **Acceptance**: `list-accounts` reads the DB; `add-account NAME` stores the
    password against a DB row (creating it if missing via `create_account`);
    `remove-account` deletes the row + clears secrets; one-shot `sync` (no
    `--account`) iterates DB accounts (mirror the daemon's
    `list_syncable_accounts`), not `cfg.accounts`; `upsert_account` can then be
    deleted entirely (no callers left). Tests cover each rewired command + a
    DB-empty / TOML-only edge case.
- **Natural moment to document the admin command surface in README**
  (`grant-admin`, `revoke-admin`, `revoke-admin-sessions`, account CRUD CLI) —
  carried-forward risk.
- **Optional**: add a CLI/UI toggle for `sync_enabled` (the daemon now honors
  it, but only `update_account`/direct SQL can set it today).

### 2. **Admin-UI Sub-plan 2A.3 / 2B / 2C** *(the larger arc)*

- **2A.3** — Jinja2/HTMX UI screens for accounts (design doc § 4). **Fold #125
  in here** (method-bound CSRF *mint* helper — verify side already method-bound
  via `csrf_action`/#122; do NOT start #125 standalone).
- **2B** — Daemon control (`DaemonSupervisor` + HTTP) — needs a
  `daemon_heartbeats` migration.
- **2C** — mbox import (`ImportWorker` + supervisor) — needs an `import_jobs`
  migration.

### 3. **Carried-forward deferred items** *(externally blocked / measured)*

- **#90** glib Cargo / Dependabot alert #3 — upstream-blocked (Tauri bump).
  The "1 moderate vulnerability" banner during `git push` is this.
- **#47** `extract_worker` transient-class opt-in — needs production telemetry.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 5** (#5, #25, #47, #90, #125). PR #132 closes no GitHub
issue (2A.2b/2A.2c were tracked in the design doc + handoff, not as issues).

## Open decisions & risks

1. **~~`sync.py:upsert_account` overwrites config columns~~ — RESOLVED this
   session (PR #132).** Now get-or-create; the daemon reads the DB and no
   longer calls it. Risk #2 retired.

2. **One-shot `localmail sync` still reads `cfg.accounts` (TOML).** It's the
   last TOML-coupled account surface and the only remaining `upsert_account`
   caller. Closes in 2A.2d (which can then delete `upsert_account`). Not a bug
   — expected.

3. **Per-account `poll_seconds` TOML override is dropped** (no DB column;
   daemon-wide `[daemon].poll_seconds` applies). Documented in CLAUDE.md and a
   `config.py` comment; the field is kept parseable for back-compat (silently
   ignored, never an error). Add a `0023` column only if an operator needs it.

4. **`sync_enabled` is honored by the daemon but has no CLI/UI toggle yet** —
   only the `update_account` service fn or direct SQL can set it. Wire a toggle
   in 2A.2d / the admin UI (2A.3).

5. **Migration numbering.** Shipped through `0022`. `0021` (`api_users_admin`)
   is taken. Next free slot is **0023**. `ls migrations/` at plan-time.

6. **README still omits admin operator commands + the new behavioral deltas.**
   Deliberate while the admin UI is mid-rollout. Revisit when 2A.2d (CLI
   rewiring) lands.

7. **`.claude/settings.local.json` stays untracked.** Local-only; the
   `gh pr create` "1 uncommitted change" warning is just this.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

# Verify state:
git status                               # clean apart from .claude/settings.local.json
git log --oneline -4                     # main tip cbfa6ec (or post-#132 merge)
git branch -vv                           # main + sub-plan-2a2b-daemon-db-source
gh pr list --state open --limit 5        # expect PR #132 open (until merged)
gh pr view 132                           # the daemon DB-source slice
gh issue list --state open --limit 40    # expect 5 open (#5, #25, #47, #90, #125)

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/        # full suite (expect 1012 passed)
unset VIRTUAL_ENV && uv run mypy src/localmail      # clean, 72 files
unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_pool.py tests/test_daemon.py  # daemon path
```

To **merge PR #132** then clean up:

```bash
gh pr merge 132 --squash --delete-branch   # or merge via the UI
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D sub-plan-2a2b-daemon-db-source
```

If picking up **Sub-plan 2A.2d** (CLI → DB):

```bash
git checkout main && git pull            # after #132 merges
git checkout -b sub-plan-2a2d-cli-db-source
# Plan first under docs/superpowers/plans/, drawing from
# docs/superpowers/specs/2026-05-28-admin-ui-design.md § 5.
# Rewire add-account / oauth-login / remove-account / list-accounts and the
# one-shot `sync` to api.admin.accounts; then delete sync.py:upsert_account
# (no callers left). Document the admin command surface in README.
unset VIRTUAL_ENV && uv run pytest -q tests/
```

## File map (post-session)

```
NEXT_SESSION.md                                          # REPLACED this session
src/localmail/daemon_accounts.py                         # NEW: pure DB-row -> AccountConfig adapter
src/localmail/daemon.py                                  # enumerates DB accounts; sizes pool from DB count
src/localmail/worker.py                                  # WorkerContext.account_id
src/localmail/api/admin/accounts.py                      # +list_syncable_accounts
src/localmail/sync.py                                    # upsert_account: get-or-create (no overwrite)
src/localmail/idle.py, poller.py                         # use ctx.account_id
src/localmail/config.py                                  # AccountConfig.poll_seconds: no-longer-consumed note
docs/superpowers/
  specs/2026-05-29-daemon-db-account-source-design.md    # NEW spec
  plans/2026-05-29-daemon-db-account-source.md           # NEW plan
docs/handoffs/
  2026-05-29T2246-utc-post-pr132-daemon-db-source.md     # NEW (this session's snapshot)
  2026-05-29T1215-utc-post-pr131-parser-typing.md        # prior
  …
```

`main` at `cbfa6ec`. Branch `sub-plan-2a2b-daemon-db-source` pushed (PR #132
open) at `ca61c07`. Working tree clean (only `.claude/settings.local.json`
untracked, by design). 2 local branches (`main`, `sub-plan-2a2b-daemon-db-source`);
1 open PR (#132).
