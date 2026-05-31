# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-31T0416 UTC.**
> This session was a short hardening slice: **Issue #140 — bound the daemon's
> fresh (non-pool) psycopg connects with `connect_timeout`**. Three daemon
> paths open a fresh `psycopg.connect(self._dsn)` — `_load_syncable_accounts`
> (startup), `reconcile` (each tick), `_clear_heartbeats` (startup reset) — and
> none passed a timeout, so a network black-hole (host up, packets dropped)
> could block them for the OS TCP default (minutes), stalling startup and
> hot-reload. Fixed via a new `DaemonConfig.db_connect_timeout_s` (int, 10) +
> a single `Daemon._connect()` helper routing all three sites. Work is on
> branch **`daemon-140-connect-timeout`** (1 commit, tip `539128e`), pushed,
> opened as **PR #141** (<https://github.com/hherb/localmail/pull/141>,
> **open, not yet merged**; **Closes #140**). Full suite **1111 passed**, mypy
> clean (78 files).
>
> The prior session's **PR #139 (2B.2 — daemon heartbeats) is MERGED**
> (`31c5ac2`); its stale local branch was deleted. The big remaining arc is
> still **2B.3–2B.5** (daemon command queue → supervisor+HTTP → admin UI).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. The daemon hot-reloads its account set (2B.1), records per-thread
heartbeats (2B.2), and now bounds its fresh connects (#140). Downstream
consumers read the DB + attachment tree directly or via the `localmail serve`
HTTPS API. See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and the 2B spec
[docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md).

## What we shipped this session

### Issue #140 — bounded fresh connects

- **`DaemonConfig.db_connect_timeout_s`** — **int** seconds, default `10`.
  Integer, *not* float, because libpq's `connect_timeout` is integer-valued
  (a float would serialise to `"10.0"` in the conninfo string). No magic
  literal. ([config.py](src/localmail/config.py))
- **NEW `Daemon._connect()` helper** ([daemon.py](src/localmail/daemon.py)) —
  `psycopg.connect(self._dsn, connect_timeout=cfg.daemon.db_connect_timeout_s)`.
  All **three** fresh-connect sites now route through it: `_load_syncable_accounts`,
  `reconcile`, `_clear_heartbeats`. The issue named only the latter two, but
  `_load_syncable_accounts` has the identical shape and is the most
  startup-critical — a *blocking* connect there stalls launch **before**
  `retry_with_backoff` can act (the retry only fires after an *exception*, not
  a hang). The single helper covers all three with no extra surface.
- **Docs**: `config.example.toml` `[daemon]` knob + README run-row clause.

### Commit on `daemon-140-connect-timeout` (1 total)

```
539128e  fix(daemon): bound fresh psycopg connects with connect_timeout (#140)
```

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1111 passed** (baseline
  1107 on merged main + 4 new: 2 config, 2 daemon-connect wiring).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 78 files**.
- TDD: wrote 4 tests, watched all 4 fail (config `AttributeError`; captured
  `connect_timeout=None`), then made them green. New test file
  `tests/test_daemon_connect_timeout.py` spies on `daemon_mod.psycopg.connect`
  and asserts every fresh connect carries the config-sourced timeout — the
  same monkeypatch pattern as `test_daemon_startup_backoff.py` (proven not to
  disturb the pool, which uses `connection_class.connect`, not module-level
  `psycopg.connect`).

## What's next

### 0. **Review & merge PR #141** *(immediate)*

PR #141 (<https://github.com/hherb/localmail/pull/141>) is **open and green**
(1111 passed, mypy clean). It **Closes #140** on merge. After merge:

```bash
gh pr merge 141 --squash --delete-branch
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D daemon-140-connect-timeout
```

### 1. **2B.3 — Command queue** *(next feature slice)*

Per [the spec](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md) §2B.3:
- Migration `0024_daemon_commands.sql` — `reload-now` / `restart-account` /
  `drain-stop` command rows.
- Daemon drains the queue on each reconcile tick (`FOR UPDATE SKIP LOCKED`) +
  optional `LISTEN/NOTIFY` for low-latency wake.
- **Enqueue accessor only** — no HTTP/CLI surface yet (that's 2B.4).
- Acceptance: a `reload-now` row makes the *next* reconcile tick converge
  immediately instead of waiting out `reload_seconds`; `restart-account N`
  tears down + respawns just account N; `drain-stop` stops the daemon
  gracefully. Poison/duplicate commands are idempotent and don't wedge the
  queue. All TDD, no magic numbers (any new timing knob → `DaemonConfig`).

Then **2B.4** (DaemonSupervisor + `/v1/admin/daemon*` routes — **must wire
`require_admin_session`**, since `get_daemon_status` has no ACL of its own by
design — + Unix control socket + `localmail daemon {status,start,stop,restart,reload}`;
this is the consumer of 2B.2's `get_daemon_status`) and **2B.5** (admin UI
panel; method-bound CSRF per #122/#125).

### 2. **Other open arcs / deferred** *(unchanged)*

- **Admin-UI Sub-plan 2A.3** (account screens; fold #125 method-bound CSRF
  mint) — independent of 2B, still open.
- Externally-blocked / measured: **#90** (glib/Tauri Dependabot), **#47**
  (extract_worker transient opt-in), **#25** (websockets.legacy depwarn),
  **#5** (search batch INSERT), **#134** (oauth_state flake — environmental).
- **Open issues: 7** (#5, #25, #47, #90, #125, #134, #140). #140 closes when
  PR #141 merges → back to 6.

## Open decisions & risks

1. **`db_connect_timeout_s` is int, not float**, by design (libpq integer
   seconds). If a future call site needs sub-second granularity it must NOT
   reuse this knob naively — libpq would truncate. Keep new fresh-connect
   sites routed through `Daemon._connect()` so the bound is never forgotten.
2. **Pool connects are NOT affected by #140.** The bound applies only to the
   three *fresh* `psycopg.connect` sites. The pool (`open_pool`,
   `self.pool.connection()`) has its own `wait=False` lazy-fill semantics
   (2B.1/#133) and is untouched — pool fills go through
   `connection_class.connect`, not module-level `psycopg.connect`, which is
   also why the monkeypatch-based tests don't disturb it.
3. **Migration numbering.** Latest applied is **0023** (daemon_heartbeats).
   Next free slot: `0024_daemon_commands.sql` (2B.3). #140 added **no**
   migration. Re-check `ls migrations/` at plan-time; never edit an
   already-applied/merged migration.
4. **Heartbeat vocabulary still load-bearing** (carried from 2B.2): any new
   heartbeat call site must use a `worker_kind`/`state` present in *both* the
   SQL CHECK lists (migration 0023) and the `WorkerKind`/`WorkerState`
   Literals in `heartbeat.py`; all loop heartbeats go through `safe_heartbeat`
   (never bare `record_heartbeat`).
5. **Tooling note** (carried): if the harness mangles/truncates tool output,
   run one command at a time and trust `pytest`/`mypy` exit signals over
   rendered text. The full-suite run emits a harmless psycopg pool `__del__`
   ResourceWarning at interpreter teardown — *not* a test failure
   (`1111 passed`).
6. **`.claude/` local files** stay untracked, by design.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/ local files
git branch -vv                           # main + daemon-140-connect-timeout (tip 539128e)
git --no-pager log --oneline -3
gh pr view 141                           # the #140 PR (open until merged)
gh issue list --state open --limit 40    # 7 open (#140 closes on merge → 6)

# Verify state (expect 1111 passed, mypy clean):
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
# This slice's tests specifically:
unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_connect_timeout.py \
    tests/test_config.py -k db_connect_timeout
```

Pick up **2B.3 (command queue)** after PR #141 merges:

```bash
git checkout main && git pull
git checkout -b daemon-control-2b3-commands
# Plan from docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md §2B.3
ls migrations/    # next slot: 0024_daemon_commands.sql
```

## File map (this session)

```
NEXT_SESSION.md                                # REPLACED this session
src/localmail/config.py                        # +DaemonConfig.db_connect_timeout_s (int=10)
src/localmail/daemon.py                         # NEW _connect() helper; 3 sites routed through it
config.example.toml                             # [daemon] db_connect_timeout_s
README.md                                       # run-row connect-timeout clause
tests/test_config.py                            # +2 knob tests (default/override)
tests/test_daemon_connect_timeout.py            # NEW — fresh-connect timeout wiring spies
docs/handoffs/2026-05-31T0416-utc-post-140-daemon-connect-timeout.md   # frozen snapshot of this file
```

`main` at `31c5ac2` (== `origin/main`, the merged 2B.2). Branch
`daemon-140-connect-timeout` at `539128e`, **pushed**
(== `origin/daemon-140-connect-timeout`), **PR #141 open**. Working tree clean
(only `.claude/` local files). 2 local branches (`main`,
`daemon-140-connect-timeout`); 1 open PR (#141).
