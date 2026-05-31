# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-05-31T0201 UTC.**
> This session shipped **2B.2 — daemon heartbeats**: every daemon worker thread
> (per-account IDLE + poll) and process-level worker (embed / extract /
> reconcile) now writes per-thread liveness to a new `daemon_heartbeats` table;
> a read-only service accessor (`get_daemon_status`) derives a `stale` flag
> purely from `now() - last_heartbeat_at`. Per the spec this slice is the
> **read accessor only** — there is **no HTTP route / CLI yet** (deferred to
> 2B.4). All work is committed and pushed on branch
> **`daemon-control-2b2-heartbeats`** (9 commits incl. the plan; tip `cd62d32`)
> and opened as **PR #139** (<https://github.com/hherb/localmail/pull/139>,
> **open, not yet merged**). Full suite **1104 passed**, mypy clean (78 files).
> Final holistic review: **ready to merge**, no Critical/Important issues.
>
> 2B.2 is **slice 2 of 5** of the re-spec'd "daemon control (2B)" arc. 2B.1
> (hot-reload) merged last session as PR #138. The remaining slices (command
> queue, supervisor+HTTP, admin-UI panel) are designed but unbuilt — see §2.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. The daemon hot-reloads its account set (2B.1) and, as of this
session, records per-thread heartbeats (2B.2). Downstream consumers read the
DB + attachment tree directly or via the `localmail serve` HTTPS API. See
[CLAUDE.md](CLAUDE.md), [README.md](README.md), and the 2B spec
[docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md).

## What we shipped this session

### 2B.2 — heartbeats (Plane A: DB-mediated, supervisor-agnostic)

Plan: [docs/superpowers/plans/2026-05-31-daemon-heartbeats-2b2.md](docs/superpowers/plans/2026-05-31-daemon-heartbeats-2b2.md)
(`034b41b`). Built TDD via subagent-driven development — each task got an
independent spec-compliance review **and** a code-quality review, then a final
holistic review.

- **Migration `0023_daemon_heartbeats.sql`** — `daemon_heartbeats` table
  (`worker_kind`, nullable `account_id BIGINT`, `state`, `current_folder`,
  `last_error_msg`, `started_at`, `last_heartbeat_at`) + two **partial unique
  indexes** (`daemon_heartbeats_acct_idx` keyed `(worker_kind, account_id)
  WHERE account_id IS NOT NULL` for account threads; `daemon_heartbeats_proc_idx`
  keyed `(worker_kind) WHERE account_id IS NULL` for process workers — each a
  valid `ON CONFLICT` target). `account_id` is **BIGINT** (matches `accounts.id`;
  caught in review — the spec sketch had `INT`). (`412ffae`)
- **NEW `src/localmail/heartbeat.py`** — `record_heartbeat` (upsert, no commit,
  branches on the two partial-index targets; `started_at` frozen on insert),
  `clear_all_heartbeats`, and `safe_heartbeat(pool, …)` — the pool-borrow
  wrapper that **NEVER raises** (logs WARNING + swallows). `WorkerKind` /
  `WorkerState` Literals match the SQL CHECK lists exactly. (`b859e3a`)
- **`DaemonConfig.heartbeat_stale_seconds`** (int, 120). (`55b3af8`)
- **NEW `src/localmail/api/admin/daemon.py`** — `get_daemon_status(conn, *,
  stale_seconds) -> DaemonStatus` (frozen `HeartbeatRow` list, `stale` derived
  in SQL via `make_interval(secs => %s)` — timezone-safe). No per-user ACL
  (operator-global; the future route is admin-gated). (`26c96cc`)
- **Daemon wiring** (`daemon.py`): startup `clear_all_heartbeats` (single-
  instance reset so a crashed predecessor's rows never read live) + a per-tick
  `reconcile` heartbeat via `safe_heartbeat(self.pool, …)` recorded **after**
  the account read so a heartbeat-write failure can't discard a good read
  (review fix). (`4003a42`)
- **idle/poll wiring** (`idle.py`, `poller.py`): idle → connecting / idle (each
  ~30s tick) / syncing / reconnecting; poll → polling / syncing+`current_folder`
  / reconnecting. (`1479295`)
- **embed/extract wiring** (`search/embed_worker.py`, `search/extract_worker.py`):
  process-level `idle` at top of each sweep + `error` on the transient/backoff
  except. (`3945a26`)
- **Docs**: `config.example.toml` `[daemon]` knob + README run-row note. (`cd62d32`)

### Commits on `daemon-control-2b2-heartbeats` (oldest → newest, 9 total)

```
034b41b  docs(2b.2): daemon heartbeats implementation plan
412ffae  feat(daemon): daemon_heartbeats table + partial unique indexes   (Task 1)
b859e3a  feat(daemon): heartbeat writer (record/clear/safe)               (Task 2)
55b3af8  feat(config): daemon.heartbeat_stale_seconds                     (Task 3)
26c96cc  feat(daemon): get_daemon_status read accessor                    (Task 4)
4003a42  feat(daemon): startup heartbeat clear + reconcile heartbeat      (Task 5)
1479295  feat(daemon): idle + poll loop heartbeats                        (Task 6)
3945a26  feat(daemon): embed + extract worker heartbeats                  (Task 7)
cd62d32  docs(daemon): document heartbeat_stale_seconds + README note     (Task 8)
```

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1104 passed** (baseline
  1081 + 23 new: 4 migration, 7 writer, 2 config, 4 reader, 6 wiring).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 78 files**.
- Per-task spec + code-quality reviews (subagents). Review fixes applied:
  `INT`→`BIGINT` FK + stronger migration tests (Task 1); test mypy guards +
  caplog/cross-account tests (Task 2); reconcile heartbeat moved out of the
  account-read transaction onto `safe_heartbeat` (Task 5). Final holistic
  review (opus): **ready to merge**.

## What's next

### 0. **Review & merge PR #139** *(immediate)*

PR #139 (<https://github.com/hherb/localmail/pull/139>) is **open and green**
(1104 passed, mypy clean, final review ready-to-merge). No GitHub issue backs
2B.2 (it came out of the re-spec), so nothing to "close". After merge:

```bash
gh pr merge 139 --squash --delete-branch
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D daemon-control-2b2-heartbeats
```

### 1. **Optional follow-ups noted by review (non-blocking)**

- **reconcile error visibility:** the `reconcile` heartbeat is written only
  after a successful `list_syncable_accounts`; a transient DB-read failure
  leaves a *stale* (not `error`) reconcile row. Acceptable (can't write if the
  DB is unreachable) — 2B.4 may emit an explicit `error` row in the except.
- **error/reconnecting wiring tests:** idle/poll `reconnecting` and
  embed/extract `error` transitions are CHECK-valid (writer-tested) but not
  asserted at the call-site spy level. Happy-path transitions are wired-tested.
- **state-label semantics:** the idle worker records `connecting` *after* the
  IMAP connection is already open; embed/extract record `idle` at the top of
  each sweep (no `running` state in the enum). Cosmetic — revisit only if the
  2B.5 admin UI surfaces state strings literally (could add a `running` /
  `connected` state in a future migration).
- **test-harness DRY:** pool-open + account-seed + WorkerContext scaffolding is
  now duplicated across `conftest.py`, `test_daemon.py`, and
  `test_daemon_heartbeats_wiring.py`. Extract a shared `tests/_worker_harness.py`
  if a fourth consumer appears.
- **`time.sleep(0.2)`** in the two process-worker wiring tests is a low-risk CI
  flake vector (the heartbeat is the first loop statement, so it fires
  immediately) — convert to a `threading.Event` set-on-first-call if it ever flakes.

### 2. **2B.3–2B.5** *(the rest of the daemon-control arc — later, each its own plan+PR)*

Per [the spec](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md), in order:
- **2B.3 — Command queue**: migration `0024_daemon_commands.sql`
  (`reload-now` / `restart-account` / `drain-stop`); daemon drains on each
  reconcile tick (`FOR UPDATE SKIP LOCKED`) + optional `LISTEN/NOTIFY`. Enqueue
  accessor only (no HTTP/CLI surface yet).
- **2B.4 — DaemonSupervisor + HTTP + CLI**: `serve/daemon_supervisor.py`
  (subprocess, Plane B), `/v1/admin/daemon*` routes (**must wire
  `require_admin_session`** — `get_daemon_status` has no ACL of its own by
  design), Unix control socket, `localmail daemon {status,start,stop,restart,reload}`.
  This is the **consumer of 2B.2's `get_daemon_status`** — its return shape
  (frozen `DaemonStatus(heartbeats=[HeartbeatRow… stale: bool])`, ordered
  `account_id NULLS LAST, worker_kind`) is already display-ready. Reuses
  `shutdown_grace_seconds` for SIGTERM→SIGKILL.
- **2B.5 — Admin UI panel**: `serve/admin/templates/daemon/panel.html` +
  dashboard card; HTMX-polled status; method-bound CSRF (#122/#125).

### 3. **Other open arcs / deferred** *(unchanged)*

- **Admin-UI Sub-plan 2A.3** (account screens; fold #125 method-bound CSRF mint)
  — independent of 2B, still open.
- Externally-blocked / measured: **#90** (glib/Tauri Dependabot), **#47**
  (extract_worker transient opt-in), **#25** (websockets.legacy depwarn),
  **#5** (search batch INSERT), **#134** (oauth_state flake — environmental).
- **Open issues: 6** (#5, #25, #47, #90, #125, #134). 2B.2 closes no issue.

## Open decisions & risks

1. **Heartbeat vocabulary is load-bearing.** Any new heartbeat call site MUST
   use a `worker_kind` and `state` already in **both** the SQL CHECK lists
   (migration 0023) **and** the `WorkerKind`/`WorkerState` Literals in
   `heartbeat.py`. A string absent from the CHECK is a runtime CHECK violation
   (caught by `safe_heartbeat`'s swallow, so it would silently *not* heartbeat).
   `starting` / `stopped` are defined but unused — reserved for 2B.4.
2. **account_id-ness selects the partial index.** Account-scoped kinds
   (`idle`, `poll`) ALWAYS pass `ctx.account_id`; process kinds (`embed`,
   `extract`, `reconcile`) ALWAYS pass `None`. Getting this wrong hits the
   wrong `ON CONFLICT` target.
3. **All loop heartbeats go through `safe_heartbeat` (never bare
   `record_heartbeat`).** This is the no-crash contract — a heartbeat write
   must never crash a sync/poll/embed/extract loop or abort a reconcile tick.
   `record_heartbeat` is only used bare in tests and inside `safe_heartbeat`.
4. **Pool sizing unchanged.** Heartbeat borrows are sub-ms upserts released
   immediately; none is held across an IMAP/IDLE wait (idle records *before*
   `idle_check`). No change to the 2B.1 `2*N + workers + headroom` formula.
5. **Staleness derived in SQL** (`now() - last_heartbeat_at > make_interval`) —
   both columns `TIMESTAMPTZ`, so no client-clock drift / tz bug. `started_at`
   is frozen on first insert (post-startup-clear), so for `reconcile` it means
   "first heartbeat", not "thread spawn".
6. **Tooling note (carried from 2B.1):** if the harness mangles/truncates tool
   output, run one command at a time and trust `pytest`/`mypy` exit signals
   over rendered text. Not observed this session.
7. **Migration numbering.** Latest applied is now **0023**. Next free slots:
   `0024_daemon_commands.sql` (2B.3) then onward. Re-check `ls migrations/` at
   plan-time. Never edit an already-applied/merged migration.
8. **Commit co-author lines:** the subagent commits carry
   `Co-Authored-By: Claude Sonnet 4.6` (the implementer model). Harmless;
   squash-merge collapses them.
9. **`.claude/` local files** stay untracked, by design.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/ local files
git branch -vv                           # main + daemon-control-2b2-heartbeats (tip cd62d32)
git --no-pager log --oneline -9          # the 2B.2 series
gh pr view 139                           # the 2B.2 PR (open until merged)
gh issue list --state open --limit 40    # 6 open

# Verify state (expect 1104 passed, mypy clean):
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
# This slice's tests specifically:
unset VIRTUAL_ENV && uv run pytest -q tests/test_migration_0023.py tests/test_heartbeat.py \
    tests/test_admin_daemon.py tests/test_daemon_heartbeats_wiring.py
```

Pick up **2B.3 (command queue)** after PR #139 merges:

```bash
git checkout main && git pull
git checkout -b daemon-control-2b3-commands
# Plan from docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md §2B.3
ls migrations/    # next slot: 0024_daemon_commands.sql
```

## File map (this session)

```
NEXT_SESSION.md                                          # REPLACED this session
docs/superpowers/plans/2026-05-31-daemon-heartbeats-2b2.md   # NEW (034b41b) — the plan
migrations/0023_daemon_heartbeats.sql                    # NEW — table + 2 partial unique indexes
src/localmail/heartbeat.py                               # NEW — record/clear/safe writer + Literals
src/localmail/api/admin/daemon.py                        # NEW — get_daemon_status read accessor
src/localmail/config.py                                  # +heartbeat_stale_seconds
src/localmail/daemon.py                                  # startup clear + reconcile heartbeat
src/localmail/idle.py                                    # idle heartbeats (connecting/idle/syncing/reconnecting)
src/localmail/poller.py                                  # poll heartbeats (polling/syncing/reconnecting)
src/localmail/search/embed_worker.py                     # embed heartbeats (idle/error)
src/localmail/search/extract_worker.py                   # extract heartbeats (idle/error)
config.example.toml                                      # [daemon] heartbeat_stale_seconds
README.md                                                # run-row heartbeat note
tests/conftest.py                                        # +daemon_heartbeats in TRUNCATE
tests/test_migration_0023.py                             # NEW — migration apply/shape/CHECK
tests/test_heartbeat.py                                  # NEW — writer upsert/clear/safe
tests/test_admin_daemon.py                               # NEW — get_daemon_status staleness/ordering
tests/test_config.py                                     # +2 knob tests
tests/test_daemon_heartbeats_wiring.py                   # NEW — reconcile/startup/idle/poll/embed/extract wiring spies
docs/handoffs/2026-05-31T0201-utc-post-2b2-daemon-heartbeats.md   # frozen snapshot of this file
```

`main` at `c5240c4` (== `origin/main`, the merged 2B.1). Branch
`daemon-control-2b2-heartbeats` at `cd62d32`, **pushed**
(== `origin/daemon-control-2b2-heartbeats`), **PR #139 open**. Working tree
clean (only `.claude/` local files). 2 local branches (`main`,
`daemon-control-2b2-heartbeats`); 1 open PR (#139).
