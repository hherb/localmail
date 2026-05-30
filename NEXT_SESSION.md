# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-30T0128 UTC.**
> This session shipped **Sub-plan 2A.2d — the CLI account commands now
> read/write the DB**, retiring the last TOML-coupled account surface and
> deleting `sync.upsert_account`. Opened as **PR #135**
> (`feat(cli): CLI account commands read/write the DB (Sub-plan 2A.2d)`),
> **open, not yet merged**. Branch `sub-plan-2a2d-cli-db-source` pushed; tip
> `96cff73`. Final whole-implementation review verdict: **READY TO MERGE** — its
> 4 non-blocking test-gap follow-ups were then **filled within this PR**
> (`96cff73`); full suite **1037 passed**, mypy clean.
>
> Reconciled remote state at session start: **PR #132 (Sub-plan 2A.2b) had
> already merged** — local `main` is at `f59a3f2`. Pruned the now-`[gone]`
> `sub-plan-2a2b-daemon-db-source` branch. Two new issues were filed since the
> prior handoff: **#133** (daemon: no startup backoff if Postgres unreachable at
> `Daemon.__init__`) and **#134** (the oauth_state tampered-signature flake,
> now tracked).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP.** The **database is now canonical
for accounts** end-to-end: the init-db TOML→DB seed (2A.2 slice 1), the daemon
(2A.2b), and now the CLI (2A.2d) all read accounts from the `accounts` table.
Downstream consumers read the DB + attachment tree directly or via the
`localmail serve` HTTPS API. The HTTPS admin UI ships under
[src/localmail/serve/admin/](src/localmail/serve/admin/) and
[src/localmail/api/admin/](src/localmail/api/admin/); end-to-end design in
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we shipped this session

**Sub-plan 2A.2d**: `list-accounts`, `add-account`, `oauth-login`,
`remove-account`, and the one-shot `localmail sync` now read/write the
`accounts` table via `api.admin.accounts` instead of `cfg.accounts` (TOML).
`sync.upsert_account` is **deleted** (no callers remain). TDD,
subagent-driven, with a final whole-implementation review (READY TO MERGE).

Spec: [docs/superpowers/specs/2026-05-30-cli-db-account-source-design.md](docs/superpowers/specs/2026-05-30-cli-db-account-source-design.md)
Plan: [docs/superpowers/plans/2026-05-30-cli-db-account-source.md](docs/superpowers/plans/2026-05-30-cli-db-account-source.md)

Commits on `sub-plan-2a2d-cli-db-source` (PR #135), oldest → newest:

```
1aed974  docs(spec): CLI account commands read/write the DB (Sub-plan 2A.2d)
7d3811d  docs(plan): CLI account commands read/write the DB (Sub-plan 2A.2d)
2796c6b  feat(admin): get_account_by_name accessor for CLI name lookup            (Task 1)
12b8151  refactor(account-seed): extract shared account_create_kwargs helper      (Task 2)
b969d64  feat(cli): pure account-name resolver (DB / seed-from-TOML / not-found)  (Task 3)
f87280c  feat(cli): list-accounts reads the DB                                    (Task 4)
87673c7  feat(cli): add-account writes to the DB, seeding from TOML when absent   (Task 5)
4ea10a6  feat(cli): oauth-login resolves the account from the DB                  (Task 6)
10f81de  feat(cli): remove-account gains --delete-row (secrets-only by default)   (Task 7)
450b7be  feat(cli): one-shot sync reads DB accounts; delete sync.upsert_account   (Task 8)
277f399  docs: CLI account commands are DB-canonical (Sub-plan 2A.2d)            (Task 9)
b1d2983  docs(handoffs): land 2026-05-30T0123 UTC post-PR-135 snapshot
825a0be  docs(handoffs): correct post-PR-135 handoff (T0123 snapshot was stale)
96cff73  test(cli): pin 4 review-flagged account-command edge cases
```

### What the change does (PR #135)

- **`list-accounts`** reads `list_accounts_full(conn)` (DB), not TOML. Shows
  name · email · endpoint (or `archive`) · auth_method · `sync=<bool>` ·
  secret status.
- **`add-account` / `oauth-login`** resolve a name via the pure
  `cli_account_resolve` (`Found` / `SeedThenUse` / `NotFound`). A name absent
  from the DB but present in `config.toml` is **seeded** via `create_account`
  + the shared `account_seed.account_create_kwargs` mapping (CLI helper
  `cli._resolve_account_row`), then the secret is stored. Wrong `auth_method`
  / unknown names fail with a clean `ClickException`.
- **`remove-account`** is **secrets-only by default** (back-compat — DB row
  untouched). `--delete-row` removes the DB row; `--force` cascades when
  messages reference it; `--force` without `--delete-row` errors; a missing
  row clears any orphaned keyring secret.
- **one-shot `sync`** (bare) iterates `list_syncable_accounts` like the
  daemon; `--account NAME` resolves via `get_account_by_name` and syncs even a
  paused (`sync_enabled = FALSE`) account, rejecting archive accounts;
  empty selection → `ClickException`.
- **`sync.sync_account`** now takes an explicit `account_id: int` (resolved by
  the caller; never creates the account row). **`sync.upsert_account`
  deleted** — `grep -rn upsert_account src/ tests/` is empty.
- `backfill-internal-date` remains TOML-driven (`_account_or_die`) — out of
  2A.2d scope.
- **New units:** `api.admin.accounts.get_account_by_name`,
  `account_seed.account_create_kwargs`, `src/localmail/cli_account_resolve.py`
  (pure), `cli._resolve_account_row`.
- **No new migration** — `0020_accounts_canonical.sql` carries every column.

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1033 passed** on the
  final run (a mid-session run showed 1032 + 1 = the #134 flake; see below).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 73 files**.
- `grep -rn "upsert_account" src/ tests/` — **empty**.
- Final whole-implementation review (subagent): spec-compliant, no
  critical/important bugs, READY TO MERGE. It flagged **4 non-blocking test
  gaps** (correct code paths, just unpinned) — **filled within this PR**
  (`96cff73`): `add-account` rejects an archive DB row; `oauth-login` seeds
  from TOML when absent; `sync --account` overrides a *paused* account;
  `sync --account` rejects an archive account. Full suite **1037 passed**.

### The #134 oauth_state flake (do NOT chase)

`test_admin_oauth_state.py::test_decode_rejects_tampered_signature` failed once
in a mid-session full-suite run but **passes 6/6 in isolation** and passed in
the final full run (1033/1033). It is a pure test (fixed key, no DB/shared
state) — tracked as the **known environmental flake #134**, unrelated to this
branch. Do not "fix" it.

### A note on harness flakiness this session

The Bash/Read tools intermittently swallowed output (identical commands
sometimes returned full output, sometimes empty; a couple of Bash calls hit
the 300s timeout). This was a **capture-layer flake, not a repo problem** —
every operation actually succeeded. The redirect-then-read-back pattern
(`cmd > /tmp/x.txt; while IFS= read -r l; do printf '%s\n' "$l"; done < /tmp/x.txt`)
was reliable when direct output was dropped. If you see empty tool returns next
session, retry via that pattern before assuming failure. (It also caused a
`git checkout -- NEXT_SESSION.md` to race a `Write` mid-session, which is why
the first handoff commit `b1d2983` captured stale content — corrected in the
next commit.)

### Docs

- **NEXT_SESSION.md** — *replaced this session* (this file).
- **docs/handoffs/2026-05-30T0128-utc-post-pr135-cli-db-source.md** — *new*
  (this file's frozen snapshot). (An earlier `…T0123…` snapshot landed with
  stale content due to the harness race above; this corrected one supersedes
  it.)
- **README.md** — updated: "Sync & accounts" now documents the DB-canonical
  model + the new `remove-account --delete-row/--force` and DB-backed
  `sync`; admin operator commands (`grant-admin` / `revoke-admin` /
  `revoke-admin-sessions`) documented (carried-forward README gap closed).
- **CLAUDE.md** — updated (2A.2d shipped; `upsert_account` deleted;
  `sync_account` takes `account_id`; remove-account secrets-only default).
- **config.py** — `AccountConfig` docstring notes TOML blocks are seed-only.
- **ROADMAP.md** — does not exist in this repo. Not created.

## What's next

### 0. **Merge PR #135** *(immediate)*

PR #135 is open and green (1037 tests, mypy clean, final review = ready to
merge; the review's 4 follow-up tests are folded in). Review + merge, then
`git fetch --prune`, fast-forward local `main`, prune
`sub-plan-2a2d-cli-db-source`.

### 1. **(Optional, small) `sync_enabled` CLI/UI setter**

- (The 4 review-flagged follow-up tests are already done — `96cff73`.)
- **`sync_enabled` CLI/UI setter.** The daemon honours `sync_enabled` and
  `sync --account` overrides it, but only `update_account` / direct SQL can
  *set* it. A `localmail enable-account` / `disable-account` (or a UI switch in
  2A.3) closes that gap. Small, high-value.

### 2. **Admin-UI Sub-plan 2A.3** *(the larger arc resumes)*

Jinja2/HTMX UI screens for accounts (design doc § 4 in
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md)).
**Fold #125 in here** (method-bound CSRF *mint* helper — verify side already
method-bound via `csrf_action`/#122; do NOT start #125 standalone).

### 3. **Fix #133 (daemon startup backoff)** *(good small slice)*

`Daemon.__init__` does a one-shot `psycopg.connect` with no retry/backoff, so
the daemon dies if Postgres is briefly down at startup. Add bounded retry
(mirror the workers' 1s→60s exponential backoff). Well-scoped warm-up task.

### 4. **Admin-UI 2B / 2C** *(later)*

- **2B** — Daemon control (`DaemonSupervisor` + HTTP) — needs a
  `daemon_heartbeats` migration.
- **2C** — mbox import (`ImportWorker` + supervisor) — needs an `import_jobs`
  migration.

### 5. **Carried-forward deferred items** *(externally blocked / measured)*

- **#90** glib Cargo / Dependabot alert #3 — upstream-blocked (Tauri bump).
  The "1 moderate vulnerability" banner during `git push` is this.
- **#47** `extract_worker` transient-class opt-in — needs production telemetry.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 7** (#5, #25, #47, #90, #125, #133, #134). PR #135 closes
no GitHub issue (2A.2d was tracked in the design doc + handoff, not as an
issue).

## Open decisions & risks

1. **~~CLI still reads `cfg.accounts` (TOML)~~ — RESOLVED this session
   (PR #135).** All account commands + one-shot `sync` are DB-canonical;
   `sync.upsert_account` deleted.

2. **`remove-account` default behaviour changed shape but not effect.** It
   never deleted the DB row before (only cleared secrets); the secrets-only
   default preserves that. `--delete-row [--force]` is the new explicit,
   guarded path. Low risk.

3. **Per-account `poll_seconds` TOML override remains dropped** (no DB column;
   daemon-wide `[daemon].poll_seconds` applies). The field is kept parseable
   for back-compat (silently ignored). Add a `0023` column only if an operator
   needs it.

4. **`sync_enabled` is honoured by the daemon + respected by `sync` but has no
   CLI/UI *setter* yet** — only `update_account` / direct SQL can set it. See
   "What's next" §1.

5. **The #134 oauth_state flake is real-but-environmental** — passes 6/6 in
   isolation; do not chase it as a regression.

6. **Migration numbering.** Shipped through `0022`; `0021` (`api_users_admin`)
   is taken. Next free slot is **0023**. 2A.2d needed NO migration.
   `ls migrations/` at plan-time.

7. **`.claude/settings.local.json` + `.claude/scheduled_tasks.lock` stay
   untracked.** Local-only; the `gh` "uncommitted change" warning is just this.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

# Verify state:
git status                               # clean apart from .claude/ local files
git log --oneline -4                     # main tip f59a3f2 (or post-#135 merge)
git branch -vv                           # main + sub-plan-2a2d-cli-db-source
gh pr list --state open --limit 5        # expect PR #135 open (until merged)
gh pr view 135                           # the CLI DB-source slice
gh issue list --state open --limit 40    # expect 7 open (#5,#25,#47,#90,#125,#133,#134)

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/        # full suite (expect 1037 passed; #134 may flake)
unset VIRTUAL_ENV && uv run mypy src/localmail      # clean, 73 files
unset VIRTUAL_ENV && uv run pytest -q tests/test_cli_accounts_db.py tests/test_cli_account_resolve.py  # this slice
```

To **merge PR #135** then clean up:

```bash
gh pr merge 135 --squash --delete-branch   # or merge via the UI
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D sub-plan-2a2d-cli-db-source
```

If picking up **Sub-plan 2A.3** (admin-UI account screens):

```bash
git checkout main && git pull            # after #135 merges
git checkout -b sub-plan-2a3-admin-ui-accounts
# Plan first under docs/superpowers/plans/, drawing from
# docs/superpowers/specs/2026-05-28-admin-ui-design.md § 4.
# Fold #125 (method-bound CSRF mint) in here.
unset VIRTUAL_ENV && uv run pytest -q tests/
```

## File map (post-session)

```
NEXT_SESSION.md                                          # REPLACED this session
docs/superpowers/specs/2026-05-30-cli-db-account-source-design.md  # NEW spec
docs/superpowers/plans/2026-05-30-cli-db-account-source.md         # NEW plan
src/localmail/cli_account_resolve.py                     # NEW: pure name resolver
src/localmail/api/admin/accounts.py                      # +get_account_by_name
src/localmail/account_seed.py                            # +account_create_kwargs (shared mapping)
src/localmail/cli.py                                     # list/add/oauth/remove/sync rewired; +_resolve_account_row
src/localmail/sync.py                                    # sync_account(account_id); upsert_account DELETED
src/localmail/config.py                                  # AccountConfig: TOML-as-seed-only docstring
README.md                                                # Sync & accounts: DB-canonical; admin commands
CLAUDE.md                                                # 2A.2d shipped invariant
tests/test_cli_account_resolve.py                        # NEW (pure resolver)
tests/test_cli_accounts_db.py                            # NEW (list/add/oauth/remove/sync over the DB)
tests/test_admin_accounts.py                             # +get_account_by_name tests
tests/test_account_seed.py                               # +account_create_kwargs test
tests/test_sync.py, tests/test_daemon.py                 # _ensure_account/_sync helpers; pass account_id; upsert tests removed
docs/handoffs/
  2026-05-30T0128-utc-post-pr135-cli-db-source.md        # NEW (this session's corrected snapshot)
  2026-05-30T0123-utc-post-pr135-cli-db-source.md        # superseded (stale content from harness race)
  2026-05-29T2246-utc-post-pr132-daemon-db-source.md     # prior
  …
```

`main` at `f59a3f2`. Branch `sub-plan-2a2d-cli-db-source` pushed (PR #135
open) at `96cff73` (this handoff commit will advance it). Working tree clean
(only `.claude/` local files untracked, by design). 2 local branches
(`main`, `sub-plan-2a2d-cli-db-source`); 1 open PR (#135).
