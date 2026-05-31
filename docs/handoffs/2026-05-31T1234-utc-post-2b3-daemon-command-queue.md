# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-31T1234 UTC.**
> Shipped **2B.3 — the daemon command queue** (Plane A), the next slice after the
> #140/#142 connect-hardening arc. A new `daemon_commands` table carries the three
> imperative actions that desired-state reconcile can't express — **`reload-now`**,
> **`restart-account`**, **`drain-stop`** — consumed by the running daemon at the
> top of each reconcile tick (`FOR UPDATE SKIP LOCKED`), with a **LISTEN/NOTIFY
> wake** so an enqueue converges immediately instead of waiting out
> `reload_seconds`. Service-layer **enqueue accessor only** — no HTTP/CLI surface
> (that's 2B.4). Work is on branch **`daemon-control-2b3-commands`** (10 commits,
> branched from `main` at `774106e`). Full suite **1137 passed**, mypy clean
> (78 files). **Not yet pushed / no PR opened** — see "What's next #0".
>
> Last session's **PR #143 (#142 statement_timeout + tcp_user_timeout) is MERGED**
> (`774106e` on main); its stale local + remote branch was deleted this session.
> The remaining 2B arc is **2B.4** (supervisor + HTTP + CLI) then **2B.5** (admin UI).

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. The daemon hot-reloads its account set (2B.1), records per-thread
heartbeats (2B.2), bounds its fresh connects on every phase (#140/#142), and now
**consumes a DB-mediated command queue** (2B.3). Downstream consumers read the DB
+ attachment tree directly or via the `localmail serve` HTTPS API. See
[CLAUDE.md](CLAUDE.md), [README.md](README.md), the 2B spec
[docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md),
and the 2B.3 plan
[docs/superpowers/plans/2026-05-31-daemon-control-2b3-command-queue.md](docs/superpowers/plans/2026-05-31-daemon-control-2b3-command-queue.md).

## What we shipped this session

### Issue/slice 2B.3 — daemon command queue (Plane A)

- **Migration `0024_daemon_commands.sql`** — `daemon_commands` table:
  `command` CHECK (`reload-now`/`restart-account`/`drain-stop`),
  `account_id` FK→accounts ON DELETE CASCADE, `state` CHECK
  (`queued`/`done`/`failed`, default `queued`), `requested_by` FK→api_users,
  `requested_at`/`picked_at`/`done_at`/`result_msg`, plus the biconditional
  CHECK `(command = 'restart-account') = (account_id IS NOT NULL)` and the
  partial index `daemon_commands_queue_idx (requested_at) WHERE state='queued'`.
  Additive-only, correctly numbered (latest applied was 0023).
- **Config (`DaemonConfig`)** — `command_listen_enabled: bool = True` and
  `command_listen_poll_seconds: float = 5.0` (the listener's `notifies()` poll /
  stop-recheck interval). No magic numbers. Documented in `config.example.toml`.
- **Service layer ([src/localmail/api/admin/daemon.py](src/localmail/api/admin/daemon.py))** —
  `DaemonCommand` dataclass + `enqueue_command` (INSERT + transactional `NOTIFY
  daemon_commands`, delivered on the caller's COMMIT), `claim_commands`
  (`FOR UPDATE SKIP LOCKED`, oldest-first, sets `picked_at`), `mark_command`
  (terminal `done`/`failed` + `done_at`; param typed `TerminalCommandState` so a
  claimed row can't be set back to `queued`). All take `conn`, none commit.
- **Daemon consumption ([src/localmail/daemon.py](src/localmail/daemon.py))** —
  `_drain_commands` (claim → apply → mark on a fresh bounded `_connect()`,
  one commit holds the lock across the batch; outer try/except swallows + retries
  next tick) and `_apply_command` (`reload-now`=no-op trigger;
  `drain-stop`=set master stop + wake; `restart-account`=teardown only — the
  **same-tick reconcile diff respawns it** since running lacks it but desired
  has it). Wired as the first two statements of `reconcile()` (drain, then
  early-return if stop set).
- **LISTEN/NOTIFY wake** — `_run_command_listener` (dedicated **autocommit**
  connection, `statement_timeout=0`, `LISTEN daemon_commands`, loops
  `notifies(timeout=command_listen_poll_seconds, stop_after=1)` setting
  `_reconcile_wake`; reconnects on error, exits on stop). `run_forever` rewritten
  to spawn the listener (gated by `command_listen_enabled`) and **wait on
  `_reconcile_wake`** instead of `stop_event.wait`; clears it each iteration and
  re-checks stop after reconcile (catches `drain-stop`). `stop()`/`_handle_signal`
  set the wake + call `_interrupt_listener()` (closes `_listener_conn`
  cross-thread so an in-flight `notifies()` unblocks at once rather than waiting
  out the poll interval — keeps shutdown latency bounded by the join, not the poll).

### Commits on `daemon-control-2b3-commands` (oldest→newest)

```
5a4188b  feat(daemon): add daemon_commands queue table (2B.3 migration)
5a1a57d  feat(daemon): add command_listen config knobs (2B.3)
73ec942  feat(daemon): enqueue/claim/mark daemon_commands service layer (2B.3)
5896f48  refactor(daemon): narrow mark_command state to terminal-only (2B.3 review)
b9b08ba  feat(daemon): consume daemon_commands at top of reconcile tick (2B.3)
736a573  docs(daemon): note lock-hold-across-join + batch rollback in _drain_commands (2B.3 review)
e6f9062  feat(daemon): LISTEN/NOTIFY wake for command queue (2B.3)
ed0e93c  test(daemon): deterministic listener-ready gate in NOTIFY tests (2B.3 review)
db490d1  docs: record 2B.3 command queue in README run row
9076d38  refactor(daemon): drop unused CommandState Literal (2B.3 final review)
```

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1137 passed** (baseline 1119
  on merged main + 18 new: 8 service, 5 consume, 3 listen, 2 config).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 78 files**.
- TDD throughout; each task got a spec-compliance + code-quality review (fixes
  folded back as `*review*` commits). Final whole-branch review: **ready to merge**.
- NOTIFY-wake tests use a deterministic listener-ready gate (`_await_listening`
  polls `d._listener_conn`) instead of a fixed sleep — re-ran 3× green, no flakes.

## What's next

### 0. **Push branch + open PR** *(immediate)*

The branch is committed but **not pushed**. Open a PR (the repo's merge pattern):

```bash
git push -u origin daemon-control-2b3-commands
gh pr create --title "feat(daemon): command queue with LISTEN/NOTIFY wake (2B.3)" --fill
# After merge:
gh pr merge <N> --squash --delete-branch
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D daemon-control-2b3-commands
```

### 1. **2B.4 — DaemonSupervisor + HTTP + CLI** *(next feature slice)*

Per [the spec](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md) §2B.4:
- `src/localmail/serve/daemon_supervisor.py` — owns `localmail run` via
  `subprocess.Popen`; `start/stop/restart/status/recent_log_lines`; state machine;
  created only if `cfg.serve.supervise_daemon` (default true), else a stub
  reporting `external`. `stop()` = SIGTERM → wait `shutdown_grace_seconds` → SIGKILL.
- Status **fuses both planes**: PID/process-state from the supervisor + per-thread
  liveness from `daemon_heartbeats` (2B.2). Externally-supervised daemons still
  report full heartbeat status read-only.
- Unix control socket at `${runtime_dir}/localmail-supervisor.sock` (0600).
- HTTP (admin-gated, **must wire `require_admin_session`** — `get_daemon_status`
  has no ACL of its own by design):
  `GET /v1/admin/daemon`, `POST /v1/admin/daemon/{start,stop,restart}` (Plane B),
  `POST /v1/admin/daemon/reload` (Plane A → **enqueue `reload-now`** via this
  session's `enqueue_command`), `POST /v1/admin/accounts/{id}/restart-sync`
  (Plane A → enqueue `restart-account`).
- CLI: `localmail daemon {status,start,stop,restart,reload}` +
  `localmail daemon restart-account NAME`. `status`/`reload`/`restart-account`
  work against the DB planes even when externally supervised; `start`/`stop`/
  `restart` require the socket and exit non-zero with the external note when
  `supervise_daemon=false`.
- **2B.4 is the consumer of this session's `enqueue_command`** — wire it, don't
  re-implement. Acceptance: the five routes + CLI parity, supervisor start/stop/
  restart/crash against a dummy subprocess, stub behaviour when supervise off,
  CSRF + admin-gate on routes (method-bound CSRF per #122/#125). All TDD, no
  magic numbers.

Then **2B.5** (admin UI panel; HTMX `hx-get` polled status, gated buttons,
method-bound CSRF).

### 2. **Other open arcs / deferred** *(unchanged)*

- **Admin-UI Sub-plan 2A.3** (account screens; fold #125 method-bound CSRF mint) —
  independent of 2B, still open.
- Externally-blocked / measured: **#90** (glib/Tauri Dependabot), **#47**
  (extract_worker transient opt-in), **#25** (websockets.legacy depwarn),
  **#5** (search batch INSERT), **#134** (oauth_state flake — environmental).
- **Open issues: 6** (#5, #25, #47, #90, #125, #134). 2B.3 had no tracking issue.

## Open decisions & risks

1. **Command queue ≠ account state.** add/remove/pause/resume stay as `accounts`
   edits the reconcile picks up; the queue is ONLY for the three imperative
   actions with no desired-state representation. Don't blur this in 2B.4.
2. **`restart-account` is teardown-then-same-tick-respawn**, NOT an explicit
   respawn in `_apply_command`. The respawn happens because the drain runs at the
   *top* of `reconcile()`, so the account diff that follows sees the account
   missing from running but present in desired. If the account read fails that
   tick (transient), the account stays down until the next successful reconcile
   (logged, best-effort) — the command is still marked `done` (teardown
   succeeded). If 2B.4's HTTP wants a stronger guarantee, note it there.
3. **Drain holds the `FOR UPDATE` lock across `_teardown_account`'s thread joins**
   (bounded by `shutdown_grace_seconds` per account; no
   `idle_in_transaction_session_timeout` set). Acceptable under the single-daemon
   model (`SKIP LOCKED` is defensive, not a clustering claim) but a DBA inspecting
   `pg_stat_activity` may see a brief idle-in-transaction — documented in the
   `_drain_commands` docstring.
4. **Listener cross-thread close is safe with psycopg 3.3** (verified in review):
   `Connection.close()` doesn't take the lock `notifies()` holds; `notifies()`
   slices its wait into ~0.1s `select()` calls and re-checks the fd, so closing
   mid-wait unblocks within ≤0.1s. `_listener_conn` is published after LISTEN and
   cleared in `finally` before any reconnect; double-close/`with`-exit races are
   benign (idempotent `close()`, swallowed). Requires **psycopg ≥3.2** for
   `notifies(stop_after=)` (repo has 3.3.4).
5. **One extra long-lived connection** when `command_listen_enabled=True`
   (default): the listener's LISTEN connection lives outside the pool. One fixed
   connection, not per-account. Disable via `[daemon] command_listen_enabled =
   false` for tight `max_connections` budgets (the poll path still consumes
   commands on the next tick).
6. **`run_forever` now waits on `_reconcile_wake`, not `stop_event.wait`.** The
   wake is set by NOTIFY (listener), `stop()`, `_handle_signal`, and `drain-stop`.
   It's cleared each iteration and stop is re-checked *after* reconcile (so
   `drain-stop`, which sets stop inside the drain, exits cleanly). Don't revert to
   `stop_event.wait` — it can't also wake on NOTIFY.
7. **Migration numbering.** Latest applied is **0024** (daemon_commands). Next
   free slot: `0025_*.sql`. 2B.4 likely needs **no** migration (supervisor + routes
   are stateless / reuse 0023+0024). Re-check `ls migrations/` at plan-time.
8. **No ROADMAP.md exists** in this repo (the 2B.3 plan's Task 6 assumed one).
   Slice status is tracked in NEXT_SESSION/handoffs + the spec, not a ROADMAP.
9. **Heartbeat vocabulary still load-bearing** (carried): any new heartbeat call
   site must use a `worker_kind`/`state` present in both the SQL CHECK lists
   (migration 0023) and the `WorkerKind`/`WorkerState` Literals in `heartbeat.py`;
   all loop heartbeats go through `safe_heartbeat`.
10. **Tooling note** (carried): the full-suite run emits a harmless psycopg pool
    `__del__` `RuntimeError: cannot join current thread` ResourceWarning at
    interpreter teardown — *not* a failure (`1137 passed`). `pytest -k FOO fileA
    fileB` applies `-k` across both files.
11. **`.claude/` local files** stay untracked, by design.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/ local files
git branch -vv                           # main + daemon-control-2b3-commands (tip 9076d38)
git --no-pager log --oneline -10
gh issue list --state open --limit 40    # 6 open

# Verify state (expect 1137 passed, mypy clean):
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
# This slice's tests specifically:
unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_commands_service.py \
    tests/test_daemon_command_consume.py tests/test_daemon_command_listen.py
unset VIRTUAL_ENV && uv run pytest -q tests/test_config.py -k command_listen
```

Pick up **2B.4 (supervisor + HTTP + CLI)** after the 2B.3 PR merges:

```bash
git checkout main && git pull
git checkout -b daemon-control-2b4-supervisor
# Plan from docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md §2B.4
ls migrations/    # likely no new migration needed; latest is 0024
```

## File map (this session)

```
NEXT_SESSION.md                                 # REPLACED this session
migrations/0024_daemon_commands.sql             # NEW — daemon_commands table + queue index
src/localmail/config.py                         # +command_listen_enabled (True) +command_listen_poll_seconds (5.0)
src/localmail/api/admin/daemon.py               # +DaemonCommand, enqueue_command, claim_commands, mark_command
src/localmail/daemon.py                         # _drain_commands/_apply_command, reconcile wiring, listener + wake loop, _interrupt_listener
config.example.toml                             # [daemon] command_listen_* knobs
README.md                                       # run-row: command queue + NOTIFY wake clause
tests/conftest.py                               # +daemon_commands in TRUNCATE list
tests/test_daemon_commands_service.py           # NEW — 8 service tests (CHECKs, NOTIFY, SKIP LOCKED, mark)
tests/test_daemon_command_consume.py            # NEW — 5 consumption tests (drain/apply effects)
tests/test_daemon_command_listen.py             # NEW — 3 listener/wake tests
tests/test_config.py                            # +2 command_listen knob tests
docs/superpowers/plans/2026-05-31-daemon-control-2b3-command-queue.md  # NEW — the implementation plan
docs/handoffs/2026-05-31T1234-utc-post-2b3-daemon-command-queue.md     # frozen snapshot of this file
```

`main` at `774106e` (== `origin/main`, the merged #142). Branch
`daemon-control-2b3-commands` at `9076d38`, **10 commits, NOT pushed, no PR yet**.
Working tree clean (only `.claude/` local files). 2 local branches (`main`,
`daemon-control-2b3-commands`); 0 open PRs.
