# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-30T0913 UTC.**
> This session fixed **#133 — daemon startup backoff**: `Daemon.__init__` now
> *waits* for Postgres (bounded exponential backoff) instead of crashing if the
> DB is briefly unreachable at launch. Shipped behind a new reusable pure-ish
> retry helper. Opened as **PR #137**
> (`fix(daemon): wait for Postgres at startup instead of crashing (#133)`),
> **open, not yet merged**. Branch `fix-133-daemon-startup-backoff` pushed.
> Full suite **1062 passed**, mypy clean (75 files).
>
> Picked up from the prior handoff's "What's next §2" (the small, well-scoped
> daemon-backoff slice). At session start PR #136 (`sync_enabled` CLI setter)
> was already **merged** into `main` (`47736a1`) and origin/main sat at
> `f3a7fc2` (post-#136 handoff commit); the stale local
> `sync-enabled-cli-setter` branch was deleted. Clean start.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres.
**Strictly read-only with respect to IMAP.** The **database is canonical for
accounts** end-to-end (init-db TOML→DB seed, daemon, and CLI all read the
`accounts` table). The daemon honours `sync_enabled` (paused accounts spawn no
threads); a CLI setter for that flag shipped last session (#136). This session
hardened daemon *startup* so a briefly-down Postgres no longer kills the
process before the per-worker reconnect loops can run. Downstream consumers
read the DB + attachment tree directly or via the `localmail serve` HTTPS API.
The HTTPS admin UI ships under [src/localmail/serve/admin/](src/localmail/serve/admin/)
and [src/localmail/api/admin/](src/localmail/api/admin/); design in
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md).
See [CLAUDE.md](CLAUDE.md), [README.md](README.md).

## What we shipped this session

**#133 — daemon startup backoff.** `Daemon.__init__` does DB IO during
construction; the **synchronous** `_load_syncable_accounts` (`psycopg.connect`,
run before the pool opens because pool sizing needs the account count) used to
raise and kill the daemon if Postgres was briefly unreachable at launch — the
existing 1s→60s backoff only lived *inside* the IDLE/poll worker loops, which
run after construction. Now that touch retries with bounded exponential
backoff, so the daemon waits for the DB rather than crash-looping under the
supervisor.

- **New `src/localmail/retry.py`** — reusable module:
  - `next_backoff(current_s, *, factor, max_s)` — **pure** `min(current*factor, max)`.
  - `retry_with_backoff(operation, *, stop_event, initial_s, max_s,
    description, factor=2.0, log=None)` — retries until the operation stops
    raising; first attempt immediate; waits on `stop_event` between attempts;
    raises `RetryAborted` the moment the event fires (stop always wins).
- **`Daemon.__init__`** wraps `_load_syncable_accounts` in `retry_with_backoff`,
  bounded by two new `DaemonConfig` knobs `startup_backoff_initial_s` (1.0) /
  `startup_backoff_max_s` (60.0). Gains an optional `stop_event` param so the
  retry can be driven/aborted in tests (and shared by future daemon control).
- **`open_pool` deliberately left plain** — it opens with `wait=False`
  (returns immediately, fills lazily on background threads) and never raises
  synchronously on an unreachable DB, so a retry wrapper there would only catch
  config errors (not transient). Caught in code review before merge; spec/docs
  reflect the reasoning.
- TDD throughout. **No migration** (config-only).

Spec: [docs/superpowers/specs/2026-05-30-daemon-startup-backoff-design.md](docs/superpowers/specs/2026-05-30-daemon-startup-backoff-design.md)
Plan: [docs/superpowers/plans/2026-05-30-daemon-startup-backoff.md](docs/superpowers/plans/2026-05-30-daemon-startup-backoff.md)

Commits on `fix-133-daemon-startup-backoff` (PR #137), oldest → newest:

```
4080252  feat(retry): reusable bounded-backoff retry helper (#133)        (Task 1)
1895981  fix(daemon): wait for Postgres at startup instead of crashing     (Task 2)
74e62b9  docs: document daemon startup backoff (#133)                      (Task 3)
```

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1062 passed**.
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 75 files**.
- New tests: `tests/test_retry.py` (7 cases) + `tests/test_daemon_startup_backoff.py`
  (2 cases: flaky-connect recovery, stop-during-backoff abort).
- Code-reviewed (agent) before merge — caught that wrapping `open_pool` was
  dead code for the connectivity case; removed it, kept the synchronous gate.

### Docs

- **NEXT_SESSION.md** — *replaced this session* (this file).
- **docs/handoffs/2026-05-30T0913-utc-post-pr137-daemon-startup-backoff.md** —
  *new* (this file's frozen snapshot).
- **README.md** (in PR #137) — `run` row: startup-backoff note.
- **CLAUDE.md** (in PR #137) — sync-model: startup-backoff paragraph.
- **config.example.toml** (in PR #137) — `[daemon]` backoff knobs.
- **ROADMAP.md** — does not exist in this repo. Not created.

## What's next

### 0. **Merge PR #137** *(immediate)*

PR #137 is open and green (1062 tests, mypy clean). Review + merge (closes
#133), then `git fetch --prune`, fast-forward local `main`, delete
`fix-133-daemon-startup-backoff`.

### 1. **Admin-UI Sub-plan 2A.3** *(the larger arc resumes)*

Jinja2/HTMX UI screens for accounts (design doc § 4 in
[docs/superpowers/specs/2026-05-28-admin-ui-design.md](docs/superpowers/specs/2026-05-28-admin-ui-design.md)).
This is where the `sync_enabled` toggle gets a UI switch — wire the UI toggle to
the same `update_account` path the CLI setter (#136) uses. **Fold #125 in here**
(method-bound CSRF *mint* helper — verify side already method-bound via
`csrf_action`/#122; do NOT start #125 standalone). Acceptance: list/create/edit/
delete accounts; per-account password / OAuth flows; a `sync_enabled` toggle;
CSRF-protected mutating routes bound to `(user_id, "<METHOD>:<action-url>")`.

### 2. **Admin-UI 2B / 2C** *(later)*

- **2B** — Daemon control (`DaemonSupervisor` + HTTP) — needs a
  `daemon_heartbeats` migration. The new `Daemon(stop_event=…)` param + the
  `retry.py` helper are stepping stones here (shared stop signal, abortable
  startup).
- **2C** — mbox import (`ImportWorker` + supervisor) — needs an `import_jobs`
  migration.

### 3. **Carried-forward deferred items** *(externally blocked / measured)*

- **#90** glib Cargo / Dependabot alert #3 — upstream-blocked (Tauri bump).
  The "1 moderate vulnerability" banner during `git push` is this.
- **#47** `extract_worker` transient-class opt-in — needs production telemetry.
- **#25** websockets.legacy DeprecationWarning — upstream-blocked.
- **#5** Search batch INSERT — deferred until measured.
- **#134** oauth_state tampered-signature flake — environmental; passes in
  isolation. Do not chase.
- **#125** method-bound CSRF *mint* — fold into Sub-plan 2A.3, not standalone.

**Open issue count after #137 merges: 6** (#5, #25, #47, #90, #125, #134).
PR #137 closes **#133**.

## Open decisions & risks

1. **`open_pool` is intentionally NOT retry-wrapped.** It opens with
   `wait=False` and never raises synchronously on a dead DB; the synchronous
   gate is `_load_syncable_accounts`. Do not "add symmetry" by wrapping the
   pool — it would be dead code for the connectivity case and only catch
   config errors that must not be retried. (Code review confirmed.)

2. **`RetryAborted` is reachable only via an injected `stop_event`.** Signal
   handlers install in `run_forever` *after* construction, so during a
   startup-backoff wait SIGTERM/SIGINT fall to the default handler (process
   exits) — correct for systemd, which owns kill semantics. The escape exists
   for tests today and daemon control (2B) later.

3. **`retry.py` is a shared primitive now.** If 2B/daemon-control wants the
   IDLE/poll loops to share it, note those loops are *run-forever* (reset
   backoff on success), not *retry-until-first-success*, so `next_backoff` is
   the reusable piece there, not `retry_with_backoff`. Consolidating the
   workers' inline `1.0`/`60.0` literals onto `next_backoff` was left out of
   scope this session.

4. **Migration numbering.** Shipped through `0022`; next free slot is **0023**.
   This session needed NO migration. `ls migrations/` at plan-time.

5. **`.claude/settings.local.json` + `.claude/scheduled_tasks.lock` stay
   untracked.** Local-only; the `gh` "uncommitted change" warning is just this.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

# Verify state:
git status                               # clean apart from .claude/ local files
git log --oneline -4 main                # main tip f3a7fc2 (or post-#137 merge)
git branch -vv                           # main + fix-133-daemon-startup-backoff
gh pr list --state open --limit 5        # expect PR #137 open (until merged)
gh pr view 137                           # the daemon startup-backoff slice
gh issue list --state open --limit 40    # 7 now; 6 after #137 closes #133

# Useful one-shots:
unset VIRTUAL_ENV && uv run pytest -q tests/        # full suite (expect 1062 passed)
unset VIRTUAL_ENV && uv run mypy src/localmail      # clean, 75 files
unset VIRTUAL_ENV && uv run pytest -q tests/test_retry.py tests/test_daemon_startup_backoff.py  # this slice
```

To **merge PR #137** then clean up:

```bash
gh pr merge 137 --squash --delete-branch   # closes #133
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D fix-133-daemon-startup-backoff
```

If picking up **Sub-plan 2A.3** (admin-UI account screens):

```bash
git checkout main && git pull            # after #137 merges
git checkout -b sub-plan-2a3-admin-ui-accounts
# Plan first under docs/superpowers/plans/, drawing from
# docs/superpowers/specs/2026-05-28-admin-ui-design.md § 4.
# Fold #125 (method-bound CSRF mint) in here. Wire the sync_enabled toggle to
# the same update_account path the CLI setter (#136) uses.
unset VIRTUAL_ENV && uv run pytest -q tests/
```

## File map (post-session)

```
NEXT_SESSION.md                                          # REPLACED this session
src/localmail/retry.py                                   # NEW (PR #137): pure next_backoff + retry_with_backoff
src/localmail/daemon.py                                  # +stop_event param, +startup retry on _load_syncable_accounts (PR #137)
src/localmail/config.py                                  # +startup_backoff_initial_s / _max_s on DaemonConfig (PR #137)
tests/test_retry.py                                      # NEW (PR #137): retry helper unit tests
tests/test_daemon_startup_backoff.py                     # NEW (PR #137): daemon flaky-connect + abort tests
README.md                                                # run-row backoff note (PR #137)
CLAUDE.md                                                # sync-model backoff paragraph (PR #137)
config.example.toml                                      # [daemon] backoff knobs (PR #137)
docs/superpowers/specs/2026-05-30-daemon-startup-backoff-design.md  # NEW spec (PR #137)
docs/superpowers/plans/2026-05-30-daemon-startup-backoff.md         # NEW plan (PR #137)
docs/handoffs/
  2026-05-30T0913-utc-post-pr137-daemon-startup-backoff.md  # NEW (this session's snapshot)
  2026-05-30T0253-utc-post-pr136-sync-enabled-setter.md      # prior
  …
```

`main` at `f3a7fc2` (== `origin/main`). Branch `fix-133-daemon-startup-backoff`
pushed (PR #137 open) at `74e62b9`. Working tree clean (only `.claude/` local
files untracked, by design). 2 local branches (`main`,
`fix-133-daemon-startup-backoff`); 1 open PR (#137).
