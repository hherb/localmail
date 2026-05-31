# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-31T0643 UTC.**
> Another short hardening slice, the natural follow-up to last session:
> **Issue #142 — bound the *query* phase of the daemon's fresh (non-pool)
> psycopg connects with `statement_timeout`**. #140 (merged as PR #141) bounded
> only the *TCP connect* phase via `connect_timeout`; a network black-hole that
> opens *after* the connect succeeds still hangs the subsequent single-row
> SELECT (`list_syncable_accounts`) or small DELETE (`clear_all_heartbeats`)
> indefinitely. Fixed via a new `DaemonConfig.db_statement_timeout_s` (int, 30;
> `0` disables) threaded into the single `Daemon._connect()` helper as libpq
> `options='-c statement_timeout=<N>s'`. Work is on branch
> **`daemon-142-statement-timeout`** (1 commit, tip `2deb658`), pushed, opened
> as **PR #143** (<https://github.com/hherb/localmail/pull/143>, **open, not yet
> merged**; **Closes #142**). Full suite **1115 passed**, mypy clean (78 files).
>
> Last session's **PR #141 (#140 connect-timeout) is MERGED** (`7dd02f7`); its
> stale local + remote branch was deleted this session. The big remaining arc is
> still **2B.3–2B.5** (daemon command queue → supervisor+HTTP → admin UI).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. The daemon hot-reloads its account set (2B.1), records per-thread
heartbeats (2B.2), and bounds its fresh connects on both the connect phase
(#140) and the query phase (#142). Downstream consumers read the DB +
attachment tree directly or via the `localmail serve` HTTPS API. See
[CLAUDE.md](CLAUDE.md), [README.md](README.md), and the 2B spec
[docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md).

## What we shipped this session

### Issue #142 — bounded statement (query) phase

- **`DaemonConfig.db_statement_timeout_s`** — **int** seconds, default `30`;
  **`0` disables** (libpq/Postgres semantics). No magic literal.
  ([config.py](src/localmail/config.py))
- **`Daemon._connect()`** ([daemon.py](src/localmail/daemon.py)) now passes
  `options=f"-c statement_timeout={cfg.daemon.db_statement_timeout_s}s"`
  alongside the existing `connect_timeout`. The **GUC unit-suffix form** (`{N}s`)
  is deliberate — it avoids any `s→ms` magic-number conversion and reads
  parallel to the `_s` config name. All three fresh-connect sites
  (`_load_syncable_accounts`, `reconcile`, `_clear_heartbeats`) inherit it
  automatically because they already route through the one helper (#140's
  single-funnel design paying off).
- **Scope**: `statement_timeout` only. The issue's "and/or TCP keepalive" was
  considered and **deliberately not done** — keepalive tuning is a distinct,
  heavier mechanism and not needed to close the stated gap. Pool connects are
  untouched (own `wait=False` lazy-fill; never go through `_connect()`).
- **Docs**: `config.example.toml` `[daemon]` knob + README run-row clause.

### Commit on `daemon-142-statement-timeout` (1 total)

```
2deb658  fix(daemon): bound fresh psycopg statement phase with statement_timeout (#142)
```

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1115 passed** (baseline
  1111 on merged main + 4 new: 2 config, 2 daemon-connect wiring).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 78 files**.
- TDD: wrote 4 tests, watched all 4 fail (config `AttributeError`; captured
  `options=None`), then made them green. Extended
  `tests/test_daemon_connect_timeout.py` — the spy now also captures
  `kwargs.get("options")` and asserts every fresh connect carries
  `-c statement_timeout=<N>s` from config.
- **End-to-end GUC confirmation** (not just a string assert): a throwaway
  `psycopg.connect(dsn, options='-c statement_timeout=13s')` →
  `SHOW statement_timeout` returned `13s`; `0s` → `0` (disabled). Proves libpq
  accepts the option *and* the server applies it.

## What's next

### 0. **Review & merge PR #143** *(immediate)*

PR #143 (<https://github.com/hherb/localmail/pull/143>) is **open and green**
(1115 passed, mypy clean). It **Closes #142** on merge. After merge:

```bash
gh pr merge 143 --squash --delete-branch
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D daemon-142-statement-timeout
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
- **Open issues: 7** (#5, #25, #47, #90, #125, #134, #142). #142 closes when
  PR #143 merges → back to 6.

## Open decisions & risks

1. **`db_statement_timeout_s` uses the GUC unit-suffix form (`{N}s`)**, not an
   `s→ms` multiply. If a future call site needs sub-second granularity it must
   NOT reuse this knob naively as integer seconds — switch the emitted form to
   `ms` and rename. Keep new fresh-connect sites routed through
   `Daemon._connect()` so *both* bounds (connect + statement) are never
   forgotten.
2. **`0` disables `statement_timeout`** (libpq/Postgres semantics) — that's the
   documented escape hatch for operators who don't want the query-phase bound.
   Don't add special-casing; `statement_timeout=0s` → `0` → disabled naturally.
3. **Pool connects are NOT affected by #140 or #142.** Both bounds apply only to
   the three *fresh* `psycopg.connect` sites via `_connect()`. The pool
   (`open_pool`, `self.pool.connection()`) has its own `wait=False` lazy-fill
   (2B.1/#133) and fills via `connection_class.connect`, not module-level
   `psycopg.connect` — which is also why the monkeypatch-based wiring tests
   don't disturb it.
4. **Migration numbering.** Latest applied is **0023** (daemon_heartbeats).
   Next free slot: `0024_daemon_commands.sql` (2B.3). #142 added **no**
   migration. Re-check `ls migrations/` at plan-time; never edit an
   already-applied/merged migration.
5. **Heartbeat vocabulary still load-bearing** (carried from 2B.2): any new
   heartbeat call site must use a `worker_kind`/`state` present in *both* the
   SQL CHECK lists (migration 0023) and the `WorkerKind`/`WorkerState`
   Literals in `heartbeat.py`; all loop heartbeats go through `safe_heartbeat`
   (never bare `record_heartbeat`).
6. **Tooling note** (carried): if the harness mangles/truncates tool output,
   run one command at a time and trust `pytest`/`mypy` exit signals over
   rendered text. The full-suite run emits a harmless psycopg pool `__del__`
   `RuntimeError: cannot join current thread` ResourceWarning at interpreter
   teardown — *not* a test failure (`1115 passed`). Also: `pytest -k FOO fileA
   fileB` applies `-k` across *both* files — run a file without `-k` to see all
   its tests.
7. **`.claude/` local files** stay untracked, by design.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/ local files
git branch -vv                           # main + daemon-142-statement-timeout (tip 2deb658)
git --no-pager log --oneline -3
gh pr view 143                           # the #142 PR (open until merged)
gh issue list --state open --limit 40    # 7 open (#142 closes on merge → 6)

# Verify state (expect 1115 passed, mypy clean):
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
# This slice's tests specifically:
unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_connect_timeout.py
unset VIRTUAL_ENV && uv run pytest -q tests/test_config.py -k "db_statement_timeout or db_connect_timeout"
```

Pick up **2B.3 (command queue)** after PR #143 merges:

```bash
git checkout main && git pull
git checkout -b daemon-control-2b3-commands
# Plan from docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md §2B.3
ls migrations/    # next slot: 0024_daemon_commands.sql
```

## File map (this session)

```
NEXT_SESSION.md                                # REPLACED this session
src/localmail/config.py                        # +DaemonConfig.db_statement_timeout_s (int=30, 0=disable)
src/localmail/daemon.py                         # _connect() now also passes options='-c statement_timeout=<N>s'
config.example.toml                             # [daemon] db_statement_timeout_s
README.md                                       # run-row statement-timeout clause
tests/test_config.py                            # +2 knob tests (default/override)
tests/test_daemon_connect_timeout.py            # +2 statement-timeout wiring spies; _cfg extended
docs/handoffs/2026-05-31T0643-utc-post-142-daemon-statement-timeout.md   # frozen snapshot of this file
```

`main` at `7dd02f7` (== `origin/main`, the merged 2B.2 + #140). Branch
`daemon-142-statement-timeout` at `2deb658`, **pushed**
(== `origin/daemon-142-statement-timeout`), **PR #143 open**. Working tree clean
(only `.claude/` local files). 2 local branches (`main`,
`daemon-142-statement-timeout`); 1 open PR (#143).
