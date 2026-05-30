# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-30T0253 UTC.**
> This session shipped the **`sync_enabled` CLI setter** — two new commands
> `localmail enable-account NAME` / `disable-account NAME` that toggle
> `accounts.sync_enabled` by name, backed by a pure planner. Opened as
> **PR #136** (`feat(cli): enable-account / disable-account sync_enabled
> setter`), **open, not yet merged**. Branch `sync-enabled-cli-setter` pushed.
> Full suite **1051 passed**, mypy clean (74 files).
>
> Picked up from the prior handoff's "What's next §1" (the small, high-value
> `sync_enabled` setter). PR #135 (Sub-plan 2A.2d) was already merged into
> `main` at session start (`7237693`); origin/main has since advanced to
> `471fb72` via handoff commits.
>
> **Git-hygiene note:** a cross-session git reset had left the working branch
> tangled; mid-session the feature commits briefly ended up on local `main`.
> This was untangled before pushing — the feature now lives **only** on
> `sync-enabled-cli-setter` (PR #136), and local `main` is clean at
> `origin/main` (`471fb72`). The spec/plan docs were lost to that reset and
> **recreated** within the PR (commit `3626f3c`). Nothing was ever force-pushed
> or lost on the remote.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP.** The **database is canonical for
accounts** end-to-end (init-db TOML→DB seed, daemon, and CLI all read the
`accounts` table). The daemon honours `sync_enabled` (paused accounts spawn no
threads); this session added the missing CLI *setter* for that flag. Downstream
consumers read the DB + attachment tree directly or via the `localmail serve`
HTTPS API. The HTTPS admin UI ships under
[src/localmail/serve/admin/](src/localmail/serve/admin/) and
[src/localmail/api/admin/](src/localmail/api/admin/); design in
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we shipped this session

**`sync_enabled` CLI setter** — `enable-account NAME` (`sync_enabled = TRUE`,
resume) and `disable-account NAME` (`sync_enabled = FALSE`, pause). DB-only name
resolution (no TOML seed — toggling presupposes the row exists); archive
accounts rejected (the daemon never syncs them); idempotent (already-in-state is
a no-op that leaves `updated_at` untouched). Pure planner
`cli_sync_toggle.plan_sync_toggle` (reject/noop/apply) behind two thin Click
commands sharing one `cli._apply_sync_toggle` helper that only calls
`update_account` on the `apply` branch. TDD, executing-plans (inline). No
migration (`sync_enabled` ships in `0020`).

Spec: [docs/superpowers/specs/2026-05-30-sync-enabled-cli-setter-design.md](docs/superpowers/specs/2026-05-30-sync-enabled-cli-setter-design.md)
Plan: [docs/superpowers/plans/2026-05-30-sync-enabled-cli-setter.md](docs/superpowers/plans/2026-05-30-sync-enabled-cli-setter.md)

Commits on `sync-enabled-cli-setter` (PR #136), oldest → newest:

```
27dfac1  feat(cli): pure planner for enable/disable-account sync toggle   (Task 1)
999617d  feat(cli): enable-account / disable-account toggle sync_enabled  (Task 2)
961b37c  docs: document enable-account / disable-account                  (Task 3)
3626f3c  docs(spec+plan): restore sync_enabled CLI setter design + plan   (recreated after git reset)
```

### What the change does (PR #136)

- **`enable-account NAME`** sets `sync_enabled = TRUE`; **`disable-account
  NAME`** sets it `FALSE`. Both resolve the name via `get_account_by_name`
  (DB-only — unknown name → `ClickException("no such account: 'NAME'")`).
- **Archive rows are rejected** either direction with a clear message (sync is
  meaningless on archive accounts; `list_syncable_accounts` already filters
  them out of the daemon).
- **Idempotent:** re-running on an account already in the target state echoes
  `account 'NAME' sync already {enabled|disabled}` and does **no** DB write
  (`updated_at` unchanged).
- **Pure planner** `cli_sync_toggle.plan_sync_toggle(*, name, auth_method,
  currently_enabled, enable) -> SyncTogglePlan(action, message)` with
  `action ∈ {reject, noop, apply}`. `cli._apply_sync_toggle` maps
  `reject → ClickException`, `noop → echo only`, `apply → update_account +
  commit + echo`.
- **New units:** `src/localmail/cli_sync_toggle.py` (pure),
  `cli._apply_sync_toggle`, `cli.enable_account`, `cli.disable_account`.
- **No new migration.**

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1051 passed** (the #134
  oauth_state flake did not surface this run).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 74 files**.
- New tests: `tests/test_cli_sync_toggle.py` (7 planner cases) + 5 DB
  integration tests in `tests/test_cli_accounts_db.py`
  (disable↔enable flip, idempotent no-bump, unknown name, archive rejected ×2).

### Docs

- **NEXT_SESSION.md** — *replaced this session* (this file, on `main`).
- **docs/handoffs/2026-05-30T0253-utc-post-pr136-sync-enabled-setter.md** —
  *new* (this file's frozen snapshot).
- **README.md** (in PR #136) — Sync & accounts table: new `enable-account` /
  `disable-account` row.
- **CLAUDE.md** (in PR #136) — Commands block + a `sync_enabled CLI setter`
  bullet in the 2A notes.
- **ROADMAP.md** — does not exist in this repo. Not created.

## What's next

### 0. **Merge PR #136** *(immediate)*

PR #136 is open and green (1051 tests, mypy clean). Review + merge, then
`git fetch --prune`, fast-forward local `main`, delete
`sync-enabled-cli-setter`.

### 1. **Admin-UI Sub-plan 2A.3** *(the larger arc resumes)*

Jinja2/HTMX UI screens for accounts (design doc § 4 in
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md)).
This is where the `sync_enabled` toggle gets a UI switch (the CLI setter shipped
this session covers the shell path; wire the UI toggle to the same
`update_account` path). **Fold #125 in here** (method-bound CSRF *mint* helper —
verify side already method-bound via `csrf_action`/#122; do NOT start #125
standalone). Acceptance: list/create/edit/delete accounts; per-account password
/ OAuth flows; a `sync_enabled` toggle; CSRF-protected mutating routes bound to
`(user_id, "<METHOD>:<action-url>")`.

### 2. **Fix #133 (daemon startup backoff)** *(good small slice)*

`Daemon.__init__` does a one-shot `psycopg.connect` with no retry/backoff, so
the daemon dies if Postgres is briefly down at startup. Add bounded retry
(mirror the workers' 1s→60s exponential backoff). Acceptance: `Daemon.__init__`
retries a connection failure with capped exponential backoff and a stop-event
escape; a unit test drives a flaky connect factory and asserts recovery without
dying.

### 3. **Admin-UI 2B / 2C** *(later)*

- **2B** — Daemon control (`DaemonSupervisor` + HTTP) — needs a
  `daemon_heartbeats` migration.
- **2C** — mbox import (`ImportWorker` + supervisor) — needs an `import_jobs`
  migration.

### 4. **Carried-forward deferred items** *(externally blocked / measured)*

- **#90** glib Cargo / Dependabot alert #3 — upstream-blocked (Tauri bump).
  The "1 moderate vulnerability" banner during `git push` is this.
- **#47** `extract_worker` transient-class opt-in — needs production telemetry.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#5** Search batch INSERT — deferred until measured.
- **#134** oauth_state tampered-signature flake — environmental; passes in
  isolation. Do not chase.

**Open issue count: 7** (#5, #25, #47, #90, #125, #133, #134). PR #136 closes
no GitHub issue (the setter was tracked in the prior handoff's "what's next",
not as an issue).

## Open decisions & risks

1. **`sync_enabled` now has a CLI setter** (this session) but **still no UI
   setter** — that lands in Sub-plan 2A.3 (a toggle on the accounts screen).
   Complementary, not redundant.

2. **Archive rejection is deliberate.** `enable-account` / `disable-account`
   refuse archive rows rather than silently setting a no-op flag. If a future
   need arises, lift the reject branch in `plan_sync_toggle` — but no daemon
   path would honour it.

3. **DB-only name resolution for the toggle** (no TOML seed) is intentional and
   differs from `add-account` / `oauth-login`, which *do* seed from TOML.
   Rationale: toggling sync presupposes the account already exists.

4. **The git tangle is resolved** — feature lives only on
   `sync-enabled-cli-setter`; local `main` == `origin/main` (`471fb72`); stale
   pre-reset commits are unreferenced and harmless.

5. **The #134 oauth_state flake is real-but-environmental** — passes in
   isolation; do not chase it as a regression.

6. **Migration numbering.** Shipped through `0022`; next free slot is **0023**.
   This session needed NO migration. `ls migrations/` at plan-time.

7. **`.claude/settings.local.json` + `.claude/scheduled_tasks.lock` stay
   untracked.** Local-only; the `gh` "uncommitted change" warning is just this.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

# Verify state:
git status                               # clean apart from .claude/ local files
git log --oneline -4 main                # main tip 471fb72 (or post-#136 merge)
git branch -vv                           # main + sync-enabled-cli-setter
gh pr list --state open --limit 5        # expect PR #136 open (until merged)
gh pr view 136                           # the sync_enabled CLI setter slice
gh issue list --state open --limit 40    # expect 7 open (#5,#25,#47,#90,#125,#133,#134)

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/        # full suite (expect 1051 passed; #134 may flake)
unset VIRTUAL_ENV && uv run mypy src/localmail      # clean, 74 files
unset VIRTUAL_ENV && uv run pytest -q tests/test_cli_sync_toggle.py tests/test_cli_accounts_db.py  # this slice
```

To **merge PR #136** then clean up:

```bash
gh pr merge 136 --squash --delete-branch   # or merge via the UI
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D sync-enabled-cli-setter
```

If picking up **Sub-plan 2A.3** (admin-UI account screens):

```bash
git checkout main && git pull            # after #136 merges
git checkout -b sub-plan-2a3-admin-ui-accounts
# Plan first under docs/superpowers/plans/, drawing from
# docs/superpowers/specs/2026-05-28-admin-ui-design.md § 4.
# Fold #125 (method-bound CSRF mint) in here. Wire the sync_enabled toggle to
# the same update_account path the CLI setter uses.
unset VIRTUAL_ENV && uv run pytest -q tests/
```

## File map (post-session)

```
NEXT_SESSION.md                                          # REPLACED this session (on main)
src/localmail/cli_sync_toggle.py                         # NEW (PR #136): pure planner
src/localmail/cli.py                                     # +enable/disable-account + _apply_sync_toggle (PR #136)
tests/test_cli_sync_toggle.py                            # NEW (PR #136): planner unit tests
tests/test_cli_accounts_db.py                            # +enable/disable DB integration (PR #136)
README.md                                                # Sync & accounts row (PR #136)
CLAUDE.md                                                # commands + setter note (PR #136)
docs/superpowers/specs/2026-05-30-sync-enabled-cli-setter-design.md  # NEW spec (PR #136)
docs/superpowers/plans/2026-05-30-sync-enabled-cli-setter.md         # NEW plan (PR #136)
docs/handoffs/
  2026-05-30T0253-utc-post-pr136-sync-enabled-setter.md  # NEW (this session's snapshot)
  2026-05-30T0128-utc-post-pr135-cli-db-source.md         # prior
  …
```

`main` at `471fb72` (== `origin/main`). Branch `sync-enabled-cli-setter` pushed
(PR #136 open) at `3626f3c`. Working tree clean (only `.claude/` local files
untracked, by design). 2 local branches (`main`, `sync-enabled-cli-setter`);
1 open PR (#136).
