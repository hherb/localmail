# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-29T0945 UTC.**
> This session shipped **Sub-plan 2A.2 slice 1 — the TOML→DB account seed at
> `init-db`** — as **PR #128**
> (`feat: TOML→DB account seed at init-db (Sub-plan 2A.2 slice 1)`),
> **open, not yet merged**. Branch `sub-plan-2a2-toml-db-account-seed`
> pushed; tip `3ec6d3c`.
>
> The session also reconciled remote state at the top: **PR #127 (#126 OAuth
> 503) had already merged** before this session opened (local `main` is at
> `b152025`), so the prior handoff's "merge PR #127" action was already done.
> Pruned the now-`[gone]` local `issue-126-…` branch (verified its only delta
> vs main was docs).

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

A focused, TDD, single-slice feature: the **init-db TOML→DB account seed**.
Eight commits on `sub-plan-2a2-toml-db-account-seed` (2 docs + 6 code/docs):

```
3ec6d3c  docs(readme): note init-db now seeds config accounts into the DB
12651e6  test(cli): pin init-db seed abort-non-zero on invalid account
1760df6  docs(claude): document the init-db TOML→DB account seed
07894f9  feat(cli): init-db seeds config.toml accounts into the DB
45dfac9  feat(seed): IO seed_accounts wrapper over the planner
948cdad  feat(seed): pure plan_account_seed planner for TOML->DB merge
00e381a  feat(admin): add public list_accounts_full accessor
ca66480  docs(plan): TOML→DB account seed at init-db (Sub-plan 2A.2 slice 1)
a260845  docs(spec): TOML→DB account seed at init-db (Sub-plan 2A.2 slice 1)
```

### What the seed does (PR #128)

- `localmail init-db` now, **after** applying migrations, merges
  `config.toml` `[[accounts]]` into the `accounts` table — idempotent, keyed
  by `name`, **DB-canonical**: existing rows are never overwritten; a drifted
  TOML value logs a WARNING naming the fields and is otherwise ignored. New
  accounts are inserted. Echoes `seeded accounts: inserted=N skipped=M
  drifted=K`.
- **Architecture (approach A from the spec):** a pure planner
  `account_seed.plan_account_seed(config_accounts, existing) → SeedPlan`
  (no IO, fully unit-tested) + a thin IO wrapper
  `account_seed.seed_accounts(conn, config_accounts, *, logger) → SeedResult`
  that reads existing rows via the new public
  `api.admin.accounts.list_accounts_full` (shares `_SELECT_FULL` with
  `get_account`), inserts via the existing `create_account` (reusing its
  validation), logs drift, returns counts.
- **Drift** compares the 8 seedable fields, maps `AccountConfig.email` →
  `email_address`, normalizes folder lists `None ≡ []`, order-sensitive.
- **Error handling:** a malformed block's `AccountFieldError` →
  `click.ClickException` (clean non-zero exit). Whole seed runs in **one
  uncommitted transaction**, so a failure leaves **no partial rows**.
- Files: `src/localmail/account_seed.py` (new), `list_accounts_full` added to
  `src/localmail/api/admin/accounts.py`, `init_db` wired in
  `src/localmail/cli.py`; tests `tests/test_account_seed.py` (14) +
  `tests/test_cli_init_db_seed.py` (3); CLAUDE.md + README notes.

### Spec / Plan (committed this session)

- Spec: [docs/superpowers/specs/2026-05-29-toml-db-account-seed-design.md](docs/superpowers/specs/2026-05-29-toml-db-account-seed-design.md)
- Plan: [docs/superpowers/plans/2026-05-29-toml-db-account-seed.md](docs/superpowers/plans/2026-05-29-toml-db-account-seed.md)
- Executed via subagent-driven development: fresh implementer per task +
  two-stage (spec then quality) review per task + a final whole-branch review
  (ready-to-merge, no high-confidence issues).

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **991 passed**, 5 warnings
  (the usual cosmetic teardown irritants: pool `__del__` thread-join, etc. —
  not product bugs).
- `unset VIRTUAL_ENV && uv run mypy src/localmail/account_seed.py
  src/localmail/api/admin/accounts.py src/localmail/cli.py` — **clean**.
- **NOTE — pre-existing whole-tree mypy debt unchanged:** `mypy src/localmail`
  still reports **4 errors in `src/localmail/parser.py`** (`union-attr` /
  `arg-type`). Longstanding, NOT from this session — see risk #1.
- `git status` — clean apart from untracked-by-design
  `.claude/settings.local.json`.

### Docs

- **NEXT_SESSION.md** — *replaced this session* (this file).
- **docs/handoffs/2026-05-29T0945-utc-post-pr128-toml-db-seed.md** — *new*
  (this file's frozen snapshot).
- **CLAUDE.md** — *updated* (init-db seed note in the Sub-plan 2A section; in PR #128).
- **README.md** — *updated* (init-db row now mentions the account seed; in PR #128).
- **ROADMAP.md** — does not exist in this repo. Not created.

## What's next

### 0. **Merge PR #128** *(immediate)*

PR #128 (`Sub-plan 2A.2 slice 1`) is open and green. Review + merge, then
`git fetch --prune`, fast-forward local `main`, and prune the local
`sub-plan-2a2-toml-db-account-seed` branch.

### 1. **Sub-plan 2A.2 — remaining slices** *(the next code work)*

From [docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md) § "New invariants" + § 5, now that the DB is
seeded/populated:

- **2A.2b — daemon reads accounts from the DB.** Switch `Daemon.__init__`
  (currently `for account in self.cfg.accounts`) to enumerate the `accounts`
  table; needs a DB-row→`WorkerContext` adapter. **Highest risk** (live sync
  path). **Retire the config-driven overwrite in `sync.py:upsert_account`** as
  part of this — see risk #2 below (it currently still overwrites
  `email/host/port/auth_method/oauth_provider` from TOML on first sync, so the
  DB is not yet *fully* canonical against the daemon).
- **2A.2c — daemon honours `sync_enabled`.** Skip rows where
  `sync_enabled=FALSE`. (Design doc § 7 framed this as v1.x; it only becomes
  meaningful once 2A.2b lands. Decide whether to fold it into 2A.2b.)
- **2A.2d — rewire CLI account commands to the DB.** `add-account` /
  `oauth-login` / `remove-account` / `list-accounts` read+write the DB via
  `api.admin.accounts` instead of `config.toml`. Medium risk (operator UX).
  Natural moment to document the admin command surface in README (risk #3).

### 2. **Admin-UI Sub-plan 2A.3 / 2B / 2C** *(the larger arc)*

- **2A.3** — Jinja2/HTMX UI screens for accounts (design doc § 4). **Fold
  #125 in here** (method-bound CSRF *mint* helper — the *verify* side is
  already method-bound via `csrf_action`/#122; do NOT start #125 standalone).
- **2B** — Daemon control (`DaemonSupervisor` + HTTP) — needs a
  `daemon_heartbeats` migration.
- **2C** — mbox import (`ImportWorker` + supervisor) — needs an `import_jobs`
  migration.

### 3. **Carried-forward deferred items** *(externally blocked)*

- **#90** glib Cargo / Dependabot alert #3 — upstream-blocked (Tauri bump).
  The "1 moderate vulnerability" banner during `git push` is this.
- **#47** `extract_worker` transient-class opt-in — needs production telemetry.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#5** Search batch INSERT — deferred until measured.

**Open issue count: 5** today (#5, #25, #47, #90, #125). (No issue tracked
slice-1 specifically; it was spec/plan-driven.)

## Open decisions & risks

1. **Pre-existing mypy errors in `src/localmail/parser.py` (4).**
   `union-attr`/`arg-type` around `_decode_part_text` / `Attachment(payload=…)`.
   Longstanding typing debt, **not** introduced this session. Worth a small
   dedicated typing-fix PR; out of scope for any admin-UI ticket. Flagged so
   the next session doesn't mistake it for a regression.

2. **`sync.py:upsert_account` still overwrites config columns (by design,
   for now).** The seed makes `accounts` populated + authoritative *for the
   admin UI / future readers*, but the running daemon still (a) reads accounts
   from `cfg.accounts`, not the DB, and (b) `ON CONFLICT DO UPDATE`s
   `email/host/port/auth_method/oauth_provider` from TOML on first sync. This
   is documented in the spec + CLAUDE.md and is **expected** — it closes when
   2A.2b rewires `Daemon.__init__` and retires that overwrite. Don't treat it
   as a bug; do retire it as part of 2A.2b.

3. **Migration numbering for 2B/2C.** Shipped through `0022`. `0021`
   (`api_users_admin`) is **taken** (the prior handoff guessed it was free —
   it isn't). Next free slot is **0023**. `ls migrations/` at plan-time; the
   design doc's `0022_import_jobs` / `0023_daemon_heartbeats` numbers must
   slide.

4. **README still omits admin operator commands** (`grant-admin`,
   `revoke-admin`, `revoke-admin-sessions`, account CRUD CLI). Deliberate
   while the admin UI is mid-rollout. Revisit when 2A.2d (CLI rewiring) lands.

5. **`.claude/settings.local.json` stays untracked.** Local-only; the
   `gh pr create` "1 uncommitted change" warning is just this.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

# Verify state:
git status                               # clean apart from .claude/settings.local.json
git log --oneline -4                     # main tip b152025 (or post-#128 merge)
git branch -vv                           # main + sub-plan-2a2-toml-db-account-seed + issue-87-…
gh pr list --state open --limit 5        # expect PR #128 open (until merged)
gh pr view 128                           # the TOML→DB seed
gh issue list --state open --limit 40    # expect 5 open

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/        # full suite (expect 991 passed)
unset VIRTUAL_ENV && uv run pytest -q tests/test_account_seed.py tests/test_cli_init_db_seed.py
unset VIRTUAL_ENV && uv run mypy src/localmail/account_seed.py src/localmail/api/admin/accounts.py src/localmail/cli.py   # clean
unset VIRTUAL_ENV && uv run mypy src/localmail      # NOTE: 4 pre-existing parser.py errors (risk #1)
```

To **merge PR #128** then clean up:

```bash
gh pr merge 128 --squash --delete-branch   # or merge via the UI
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D sub-plan-2a2-toml-db-account-seed
```

If picking up **Sub-plan 2A.2b** (daemon reads accounts from DB):

```bash
git checkout -b sub-plan-2a2b-daemon-db-source
# Plan first under docs/superpowers/plans/, drawing from
# docs/superpowers/specs/2026-05-28-admin-ui-design.md.
# Switch Daemon.__init__ to enumerate the accounts table; build a
# DB-row -> WorkerContext adapter; retire sync.py:upsert_account's config
# overwrite (risk #2). Highest-risk slice — live sync path.
unset VIRTUAL_ENV && uv run pytest -q tests/
```

## File map (post-session)

```
NEXT_SESSION.md                                          # REPLACED this session
src/localmail/account_seed.py                            # NEW (PR #128) — planner + IO seed
src/localmail/api/admin/accounts.py                      # +list_accounts_full (PR #128)
src/localmail/cli.py                                     # init_db seeds after migrations (PR #128)
tests/test_account_seed.py                               # NEW (PR #128) — 14 tests
tests/test_cli_init_db_seed.py                           # NEW (PR #128) — 3 tests
CLAUDE.md                                                # +seed note (PR #128)
README.md                                                # init-db row updated (PR #128)
docs/superpowers/specs/2026-05-29-toml-db-account-seed-design.md   # NEW (spec)
docs/superpowers/plans/2026-05-29-toml-db-account-seed.md          # NEW (plan)
docs/handoffs/
  2026-05-29T0945-utc-post-pr128-toml-db-seed.md         # NEW (this session's snapshot)
  2026-05-29T0412-utc-post-pr127-oauth-503.md            # prior
  …
```

`main` at `b152025`. Branch `sub-plan-2a2-toml-db-account-seed` pushed (PR
#128 open) at `3ec6d3c`. Working tree clean (only
`.claude/settings.local.json` untracked, by design). 3 local branches
(`main`, `sub-plan-2a2-toml-db-account-seed`, `issue-87-…`); 1 open PR (#128).
