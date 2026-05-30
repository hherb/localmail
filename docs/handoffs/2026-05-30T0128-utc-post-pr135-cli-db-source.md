# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-30T0233 UTC.**
> **Sub-plan 2A.2d — CLI account commands read/write the DB — is SHIPPED and
> MERGED.** PR #135 squash-merged into `main` (tip `7237693`). Local `main` is
> up to date with `origin/main`; the feature branch is gone. Working tree clean
> apart from the untracked local `.claude/scheduled_tasks.lock`.
>
> Full suite **1039 passed**, mypy clean (73 files), `grep -rn upsert_account
> src/ tests/` empty. The `add-account` seed-rollback fix and its regression
> test (`test_add_account_seed_mismatch_does_not_persist_row`) are part of the
> merged PR.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP.** The **database is now canonical
for accounts** end-to-end: the init-db TOML→DB seed (2A.2 slice 1), the daemon
(2A.2b), and the CLI (2A.2d, merged this session) all read accounts from the
`accounts` table. Downstream consumers read the DB + attachment tree directly
or via the `localmail serve` HTTPS API. The HTTPS admin UI ships under
[src/localmail/serve/admin/](src/localmail/serve/admin/) and
[src/localmail/api/admin/](src/localmail/api/admin/); end-to-end design in
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What shipped this session (PR #135, merged → `7237693`)

`list-accounts`, `add-account`, `oauth-login`, `remove-account`, and the
one-shot `localmail sync` now read/write the `accounts` table via
`api.admin.accounts` instead of `cfg.accounts` (TOML). `sync.upsert_account`
is **deleted** (no callers remain). Built TDD / subagent-driven with a final
whole-implementation review (verdict: ready to merge).

Spec: [docs/superpowers/specs/2026-05-30-cli-db-account-source-design.md](docs/superpowers/specs/2026-05-30-cli-db-account-source-design.md)
Plan: [docs/superpowers/plans/2026-05-30-cli-db-account-source.md](docs/superpowers/plans/2026-05-30-cli-db-account-source.md)

### What the change does

- **`list-accounts`** reads `list_accounts_full(conn)` (DB), not TOML.
- **`add-account` / `oauth-login`** resolve a name via the pure
  `cli_account_resolve` (`Found` / `SeedThenUse` / `NotFound`). A name absent
  from the DB but present in `config.toml` is seeded via `create_account` +
  the shared `account_seed.account_create_kwargs` mapping (CLI helper
  `cli._resolve_account_row`), then the secret is stored. **`add-account`
  validates `auth_method` BEFORE `conn.commit()`** so a rejected seed-from-TOML
  row (oauth2/archive) rolls back — a failed command leaves no DB row.
- **`remove-account`** is secrets-only by default; `--delete-row` removes the
  DB row, `--force` cascades when messages reference it.
- **one-shot `sync`** (bare) iterates `list_syncable_accounts`; `--account
  NAME` overrides a paused (`sync_enabled = FALSE`) account, rejects archive.
- **`sync.sync_account`** takes an explicit `account_id: int`;
  `sync.upsert_account` deleted.
- New units: `api.admin.accounts.get_account_by_name`,
  `account_seed.account_create_kwargs`, `src/localmail/cli_account_resolve.py`
  (pure), `cli._resolve_account_row`. No new migration (0020 has every column).
- Docs updated in the PR: README ("Sync & accounts" DB-canonical + admin
  commands), CLAUDE.md (2A.2d invariant), config.py `AccountConfig` docstring.

### The #134 oauth_state flake (do NOT chase)

`test_admin_oauth_state.py::test_decode_rejects_tampered_signature` can fail
once in a full-suite run but passes 6/6 in isolation — pure test, no
DB/shared state. Tracked as environmental flake **#134**. Don't "fix" it.

## What's next

### 1. Small, high-value: `sync_enabled` CLI/UI setter

The daemon honours `sync_enabled` and `sync --account` overrides it, but only
`update_account` / direct SQL can *set* it. Add `localmail enable-account` /
`disable-account` (or a UI switch in 2A.3). Small.

### 2. Fix #133 — daemon startup backoff

`Daemon.__init__` does a one-shot `psycopg.connect` with no retry/backoff, so
the daemon dies if Postgres is briefly down at startup. Add bounded retry
(mirror the workers' 1s→60s exponential backoff). Well-scoped warm-up task.

### 3. Admin-UI Sub-plan 2A.3 (the larger arc)

Jinja2/HTMX UI screens for accounts (design doc § 4 in
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md)).
**Fold #125 in here** (method-bound CSRF *mint* helper — verify side already
method-bound via `csrf_action`/#122; do NOT start #125 standalone).

### 4. Admin-UI 2B / 2C (later)

- **2B** — Daemon control (`DaemonSupervisor` + HTTP) — needs a
  `daemon_heartbeats` migration.
- **2C** — mbox import (`ImportWorker` + supervisor) — needs an `import_jobs`
  migration.

### 5. Carried-forward deferred items (externally blocked / measured)

- **#90** glib Cargo / Dependabot alert #3 — upstream-blocked (Tauri bump).
  The "1 moderate vulnerability" push banner is this.
- **#47** `extract_worker` transient-class opt-in — needs production telemetry.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 7** (#5, #25, #47, #90, #125, #133, #134).

## Open decisions & risks

1. **Process note (this session):** PR #135 was merged by the user mid-flow,
   after which a stale `/loop` wakeup fired and this agent briefly continued on
   `main`. It made a few **doc-only** commits to `main` (NEXT_SESSION.md +
   handoff snapshot: `6792dc3`, `99ec286`, `2538593`, plus this correction) —
   no code changes (the seed-rollback fix was already in the merge). Harmless,
   but a reminder: **branch before committing to `main`**; ignore stale `/loop`
   wakeups once the work is merged.

2. **`remove-account` default** clears secrets only (DB row untouched) — matches
   pre-2A.2d behaviour. `--delete-row [--force]` is the explicit guarded path.

3. **Per-account `poll_seconds` TOML override remains dropped** (daemon-wide
   `[daemon].poll_seconds` applies). Field kept parseable, silently ignored.
   Add a `0023` column only if an operator needs it.

4. **`sync_enabled` has no CLI/UI setter yet** — see "What's next" §1.

5. **Migration numbering.** Shipped through `0022`; next free slot is **0023**.
   `ls migrations/` at plan-time.

6. **`.claude/settings.local.json` + `.claude/scheduled_tasks.lock`** stay
   untracked (local-only).

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first
git status                               # clean apart from .claude/ local files
git log --oneline -4                     # main tip 7237693 (PR #135 merged)
gh pr list --state open --limit 5        # expect none of ours
gh issue list --state open --limit 40    # expect 7 open (#5,#25,#47,#90,#125,#133,#134)

unset VIRTUAL_ENV && uv run pytest -q tests/        # full suite (expect 1039 passed; #134 may flake)
unset VIRTUAL_ENV && uv run mypy src/localmail      # clean, 73 files
```

To pick up the next slice (e.g. `sync_enabled` toggle or #133):

```bash
git checkout main && git pull
git checkout -b <slice-branch>           # always branch off main; never commit to main directly
# brainstorm → spec → plan → TDD per the superpowers workflow
unset VIRTUAL_ENV && uv run pytest -q tests/
```

## File map (merged in PR #135)

```
src/localmail/cli_account_resolve.py     # pure name resolver (Found/SeedThenUse/NotFound)
src/localmail/api/admin/accounts.py      # +get_account_by_name
src/localmail/account_seed.py            # +account_create_kwargs (shared mapping)
src/localmail/cli.py                     # list/add/oauth/remove/sync rewired; +_resolve_account_row;
                                         #   add-account validates auth_method BEFORE commit (seed rollback)
src/localmail/sync.py                    # sync_account(account_id); upsert_account DELETED
src/localmail/config.py                  # AccountConfig: TOML-as-seed-only docstring
README.md, CLAUDE.md                     # DB-canonical CLI documented
tests/test_cli_account_resolve.py        # pure resolver tests
tests/test_cli_accounts_db.py            # list/add/oauth/remove/sync over the DB + seed-rollback test
tests/test_admin_accounts.py             # +get_account_by_name tests
tests/test_account_seed.py               # +account_create_kwargs test
tests/test_sync.py, tests/test_daemon.py # _ensure_account/_sync helpers; pass account_id
docs/superpowers/specs/2026-05-30-cli-db-account-source-design.md   # spec
docs/superpowers/plans/2026-05-30-cli-db-account-source.md          # plan
docs/handoffs/2026-05-30T0128-utc-post-pr135-cli-db-source.md       # frozen snapshot
```

`main` at `7237693` (PR #135 merged), synced with `origin/main`. No open PRs of
ours. Working tree clean (only `.claude/` local files untracked).
