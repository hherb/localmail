# Daemon control (2B) — full re-spec

**Status:** draft for sign-off, 2026-05-30.
**Author:** Horst Herb, with Claude (brainstorming session).
**Supersedes:** §2B ("Daemon control") of
[2026-05-28-admin-ui-design.md](2026-05-28-admin-ui-design.md). The
`daemon_heartbeats` sketch in §1 of that document is replaced by the schema
here. Everything else in the admin-UI design (2A accounts, 2C import, auth)
stands unchanged.

## Why re-spec

The approved §2B modelled "daemon control" as a **subprocess supervisor**:
`serve` owns `localmail run` as a child process and account changes are picked
up by **restarting** the whole daemon. There is no live reload. In practice the
operator-facing need is the opposite: add / remove / pause / resume an account
and have the running daemon converge **without a restart** — restarting drops
every account's open IDLE connection and re-walks every INBOX backlog just to
change one account.

This re-spec keeps the subprocess supervisor (it is still the right tool for
*process lifecycle*) but adds the missing piece — a **DB-mediated reconcile
engine** — and the observability + imperative-control surfaces that make the
whole thing operable.

## Architecture: two control planes

The daemon and `serve` still **share Postgres and can run independently**
(the long-standing invariant). Control splits into two planes:

### Plane A — DB-mediated (supervisor-agnostic, always available)

Works whether the daemon runs under `serve`, under systemd, or by hand.

- **Hot-reload / reconcile.** The daemon polls the `accounts` table every
  `reload_seconds`, diffs the desired set against the running threads by
  `(account_id, updated_at)`, and spawns / tears-down / respawns per-account
  thread bundles. The pool is resized on each reconcile that changed the
  account count. *This is the engine that delivers "no restart".*
- **Heartbeats.** Every worker thread writes liveness to `daemon_heartbeats`;
  `serve` / CLI read it for "is it actually doing work?" status independent of
  PID-alive checks.
- **Command queue.** A `daemon_commands` table carries imperative actions that
  are **not** expressible as desired state — `reload-now`, `restart-account`,
  `drain-stop`. Account add / remove / pause / resume are **not** commands;
  they are `accounts`-table edits the reconcile picks up.

### Plane B — process lifecycle (only when `serve` supervises)

The approved subprocess model. `DaemonSupervisor` owns `localmail run` via
`subprocess.Popen` and exposes start / stop / restart plus a Unix control
socket for the CLI. When `[serve] supervise_daemon = false` (systemd / launchd),
this plane is **read-only** — the OS owns lifecycle — and Plane A still works in
full.

```
            Admin browser / CLI
                   │
        ┌──────────┴───────────┐
        │ serve (FastAPI)      │     Plane B (lifecycle, optional)
        │  DaemonSupervisor ───┼────────────► localmail run (child proc)
        └──────────┬───────────┘                     │
                   │ reads heartbeats,                │ Plane A (always):
                   │ writes commands                  │  • reconcile accounts
                   ▼                                  │  • write heartbeats
                PostgreSQL ◄───────────────────────────  • consume commands
        (accounts · daemon_heartbeats · daemon_commands)
```

## Slice decomposition

One design, five shippable slices. Each gets its own implementation-plan phase
and its own PR; later slices depend on earlier ones.

| Slice | Scope | Migration | Touches `serve`? |
|------|-------|-----------|------------------|
| **2B.1** | Reconcile engine (hot-reload) | none | no |
| **2B.2** | Heartbeats | `0023_daemon_heartbeats.sql` | read accessor only |
| **2B.3** | Command queue | `0024_daemon_commands.sql` | enqueue accessor only |
| **2B.4** | DaemonSupervisor + HTTP + CLI | none | yes |
| **2B.5** | Admin UI panel | none | yes |

**Build 2B.1 first** — it is the foundation and is independently valuable
(account changes without restart) even if nothing else ships.

---

## 2B.1 — Reconcile engine (hot-reload)

### Today

`Daemon.__init__` reads the account set **once** (`_load_syncable_accounts` →
`list_syncable_accounts`) and `run_forever` spawns per-account IDLE + poll
threads that **all share the single master `stop_event`**. There is no way to
stop one account's threads without stopping the daemon, and the account set is
frozen for the life of the process.

### Per-account stop events

The master `self._stop_event` keeps its meaning — *"the whole daemon is
shutting down"* — and continues to drive the embed / extract workers. We add a
**per-account** event so one account can be torn down independently. The IDLE /
poll loops are unchanged: they already only check `ctx.stop` (the
`WorkerContext`'s event), so we simply hand each account bundle its own event
instead of the shared master.

```python
@dataclass
class AccountThreads:
    account_id: int
    updated_at: datetime          # fingerprint this bundle was spawned with
    stop_event: threading.Event   # set on teardown OR on daemon shutdown
    idle_thread: threading.Thread
    poll_thread: threading.Thread
```

`Daemon` holds `self._account_threads: dict[int, AccountThreads]`.

- **spawn(row):** fresh `Event`; build `WorkerContext(stop=event, …)`; start
  idle + poll; register.
- **teardown(account_id):** `event.set()`; `join(timeout=
  daemon.shutdown_grace_seconds)` both threads; drop from the registry. Threads
  are `daemon=True`, so an IDLE thread still blocked inside its
  `idle_renew_seconds` window does not wedge shutdown — it exits at its next
  wake. (Teardown latency is therefore bounded by the IDLE heartbeat tick,
  ~`HEARTBEAT_SECONDS`, not by `idle_renew_seconds`, because `_idle_step`
  re-checks `ctx.stop` every `HEARTBEAT_SECONDS`.)
- **respawn(row):** teardown then spawn (config changed).
- On daemon shutdown (master event set): tear down every registered account,
  then join the embed / extract workers (which observe the master event
  directly).

### Pure reconcile planner

New module `src/localmail/daemon_reconcile.py` (pure: no IO, no threads — same
shape as `daemon_accounts.py`), unit-testable in isolation:

```python
@dataclass(frozen=True)
class ReconcilePlan:
    to_spawn:    tuple[int, ...]   # in desired, not running
    to_teardown: tuple[int, ...]   # running, not in desired
    to_respawn:  tuple[int, ...]   # in both, updated_at differs

    @property
    def is_empty(self) -> bool: ...

def plan_reconcile(
    running: Mapping[int, datetime],   # account_id -> updated_at last spawned with
    desired: Mapping[int, datetime],   # account_id -> updated_at from the DB
) -> ReconcilePlan: ...
```

The `(account_id, updated_at)` key captures all three transitions: appear (new
id), disappear (id gone from the syncable set — deleted, paused, or switched to
`archive`), and config edit (same id, newer `updated_at`). The diff needs only
*inequality*, so writer clock skew is harmless.

### Daemon reconcile loop

`run_forever` becomes a reconcile loop instead of a bare `stop_event.wait()`:

```
install signals
start embed / extract worker threads (once, on the master event)   # unchanged
reconcile()                                  # initial spawn of account bundles
while not self._stop_event.wait(reload_seconds):   # wakes early on signal
    reconcile()
teardown_all_accounts()                      # master event fired
join worker threads; pool.close()
```

`Daemon.reconcile()` (the IO wrapper):

1. Read desired rows via `list_syncable_accounts`. **A transient DB failure is
   logged (WARNING) and swallowed for that tick** — existing threads keep
   running; retry next tick. We do not wrap this in `retry_with_backoff` (that
   helper retries until *first* success; the reconcile loop is already a
   periodic retry). A persistent outage just means no convergence until the DB
   returns; running threads keep working / backing off on their own.
2. Build `desired = {row.id: row.updated_at}` + `rows_by_id = {row.id: row}`.
3. `plan = plan_reconcile(self._running_fingerprints(), desired)`.
4. Apply **teardown → respawn → spawn** (teardown first frees pool slots).
5. If the account count changed, `pool.resize(min_size, max_size)` to the
   recomputed target (`compute_daemon_pool_size(...)` capped by
   `daemon.pool_max_size`). psycopg_pool supports `resize`.
6. `log()` a one-line summary when the plan was non-empty
   (`reconcile: spawned=… torn_down=… respawned=…`).

### Config

New fields on `DaemonConfig` (`extra="forbid"`, documented in
`config.example.toml`):

```python
reload_seconds: int = 30          # how often the daemon re-reads the account set
shutdown_grace_seconds: float = 30  # per-thread join timeout on teardown / shutdown
```

`shutdown_grace_seconds` replaces the hardcoded `join(timeout=10)` in
`run_forever` and is reused by 2B.4's `DaemonSupervisor.stop()` (SIGTERM → wait
this long → SIGKILL). Introduced here because per-account teardown needs a
bounded join.

### Credential-refresh participation (decision)

`store_password` / `complete_oauth` write only to the keyring, **not** to
`accounts.updated_at` — so an OAuth re-login or password rotation would *not*
trigger a respawn under the `(id, updated_at)` diff, and the daemon would keep
using the stale credential until the next unrelated edit or restart.

**Decision:** the secret-store service paths bump `accounts.updated_at` so a
credential change participates in hot-reload. `complete_oauth` already holds a
`conn`; add a single `UPDATE accounts SET updated_at = now() WHERE id = %s`
after `set_refresh_token`. `store_password` currently takes an `Account` and no
`conn`; give it a `conn` (the CLI / admin callers already have one) and do the
same touch. This is part of 2B.1 — without it, "hot-reload" silently excludes
the most security-relevant change.

### Concurrency / correctness notes

- **Signal during a reconcile wait:** `run_forever` blocks in
  `self._stop_event.wait(reload_seconds)`; the signal handler sets the master
  event, which both wakes the wait and is the shutdown sentinel — clean exit.
- **A crashed loop:** if an account's idle / poll loop exited on its own, the
  registry still holds a dead `Thread`; the fingerprint diff won't respawn it
  (id still "running"). This matches today's behaviour and is out of 2B.1
  scope — 2B.2 heartbeats make the staleness visible; auto-respawn-on-death is
  future work.
- **`reload_seconds` floor:** not clamped (consistent with the other daemon
  knobs); a foolish `0` is the operator's call. Documented.

### Tests (TDD)

- `tests/test_daemon_reconcile.py` (pure): spawn-only, teardown-only,
  respawn-on-`updated_at`-change, no-op when identical, combined plan, empty
  both sides.
- `tests/test_daemon_hot_reload.py` (orchestration; mock style of
  `test_daemon_startup_backoff.py` — patch `list_syncable_accounts`,
  `run_inbox_idle_loop`, `run_poll_loop`): add → +2 threads, registry grows;
  remove → its event set, threads joined, *other* accounts untouched; edit →
  old torn down + new spawned; DB read raises → existing threads survive +
  WARNING; master stop mid-wait → all per-account events set, clean join; pool
  `resize` called with recomputed size when count changed, not when unchanged.
- Config round-trip for `reload_seconds` (default + `extra="forbid"`).
- Credential-refresh touch: `store_password` / `complete_oauth` bump
  `updated_at` (service-layer DB test).

### Files (2B.1)

```
src/localmail/daemon_reconcile.py     # NEW — pure ReconcilePlan + plan_reconcile
src/localmail/daemon.py               # per-account events, reconcile loop, resize
src/localmail/config.py               # +reload_seconds, +shutdown_grace_seconds
src/localmail/api/admin/accounts.py   # store_password gains conn; bumps updated_at
src/localmail/api/admin/oauth.py      # complete_oauth bumps updated_at
config.example.toml                   # [daemon] reload_seconds
tests/test_daemon_reconcile.py        # NEW
tests/test_daemon_hot_reload.py       # NEW
```

No migration (`updated_at` already exists in 0020).

---

## 2B.2 — Heartbeats

### Migration `0023_daemon_heartbeats.sql`

The approved sketch used `account_id` as the sole PK with a `thread_kind`
column — but each account runs **two** threads (idle + poll), so one row per
account cannot represent both, and there are also process-level workers
(embed / extract / the reconcile loop) with no account. Replacement:

```sql
CREATE TABLE daemon_heartbeats (
  id                BIGSERIAL PRIMARY KEY,
  worker_kind       TEXT NOT NULL
                    CHECK (worker_kind IN ('idle','poll','embed','extract','reconcile')),
  account_id        INT REFERENCES accounts(id) ON DELETE CASCADE,  -- NULL for process-level
  state             TEXT NOT NULL
                    CHECK (state IN ('starting','connecting','idle','polling',
                                     'syncing','error','reconnecting','stopped')),
  current_folder    TEXT,
  last_error_msg    TEXT,
  started_at        TIMESTAMPTZ NOT NULL,
  last_heartbeat_at TIMESTAMPTZ NOT NULL
);

-- one row per (account thread) and one per (process-level worker):
CREATE UNIQUE INDEX daemon_heartbeats_acct_idx
  ON daemon_heartbeats (worker_kind, account_id) WHERE account_id IS NOT NULL;
CREATE UNIQUE INDEX daemon_heartbeats_proc_idx
  ON daemon_heartbeats (worker_kind) WHERE account_id IS NULL;
```

Two partial unique indexes (instead of `UNIQUE NULLS NOT DISTINCT`) keep this
Postgres-version-agnostic. Each is a valid `ON CONFLICT` target.

### Writer

A small `src/localmail/heartbeat.py` helper: `record_heartbeat(conn, *,
worker_kind, account_id, state, current_folder=None, last_error_msg=None)`
does the upsert against the right partial index. The IDLE / poll / embed /
extract loops call it at the top of each iteration and on state transitions
(connect, sync, error, reconnect). The reconcile loop records its own
`worker_kind='reconcile'` row each tick.

**Single-instance assumption** (multi-host clustering is a stated non-goal):
on startup the daemon `DELETE`s all heartbeat rows once, so leftover rows from
a previous (crashed) run never read as live. Staleness is then purely
`now() - last_heartbeat_at > daemon.heartbeat_stale_seconds` (new knob, default
120s).

### Reader

`src/localmail/api/admin/daemon.py` (service layer, no FastAPI):
`get_daemon_status(conn, *, stale_seconds) -> DaemonStatus` returns the
heartbeat rows annotated with a derived `stale: bool`. No ACL — daemon status
is operator-global (the route is admin-gated).

### Tests (2B.2)

Migration apply test; `record_heartbeat` upsert (insert then update same row);
both partial-index conflict targets; `get_daemon_status` staleness derivation;
startup `DELETE` clears stale rows.

---

## 2B.3 — Command queue

### Migration `0024_daemon_commands.sql`

```sql
CREATE TABLE daemon_commands (
  id           BIGSERIAL PRIMARY KEY,
  command      TEXT NOT NULL
               CHECK (command IN ('reload-now','restart-account','drain-stop')),
  account_id   INT REFERENCES accounts(id) ON DELETE CASCADE,  -- required iff restart-account
  state        TEXT NOT NULL DEFAULT 'queued'
               CHECK (state IN ('queued','done','failed')),
  requested_by INT REFERENCES api_users(id),
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  picked_at    TIMESTAMPTZ,
  done_at      TIMESTAMPTZ,
  result_msg   TEXT,
  CHECK ((command = 'restart-account') = (account_id IS NOT NULL))
);
CREATE INDEX daemon_commands_queue_idx
  ON daemon_commands (requested_at) WHERE state = 'queued';
```

### Semantics

- **`reload-now`** — force an immediate reconcile rather than waiting out
  `reload_seconds`.
- **`restart-account`** — teardown + respawn one account's bundle now (operator
  remedy for a wedged connection). `account_id` required.
- **`drain-stop`** — set the master stop event; the daemon drains and exits.
  (Process *start* is Plane B only — a stopped daemon cannot consume a
  `start` command.)

### Consumption

The daemon drains the queue at the top of each reconcile tick:
`SELECT … WHERE state='queued' ORDER BY requested_at FOR UPDATE SKIP LOCKED`,
acts, then marks `done` / `failed` with `result_msg`. For immediacy it also
`LISTEN daemon_commands`; an enqueue path `NOTIFY`s, and the reconcile wait
becomes "wait on the master event OR a notification, up to `reload_seconds`".
(Implementation: a dedicated short-lived listener connection whose arrival sets
a `threading.Event` the main loop also waits on. Detailed in the 2B.3 plan;
the poll path alone is correct, NOTIFY only reduces latency.)

Enqueue accessor: `api/admin/daemon.py::enqueue_command(conn, *, command,
account_id=None, requested_by) -> int` + `NOTIFY`.

### Tests (2B.3)

CHECK constraint (`restart-account` requires `account_id`; others forbid it);
`FOR UPDATE SKIP LOCKED` single-consumer; each command's effect against a faked
daemon registry; `reload-now` wakes the wait early.

---

## 2B.4 — DaemonSupervisor + HTTP + CLI

Carries over the approved §2B largely intact, layered on the DB planes above.

- `src/localmail/serve/daemon_supervisor.py` — owns `localmail run` via
  `subprocess.Popen`; `start / stop / restart / status / recent_log_lines`;
  state machine `stopped → starting → running → stopping → stopped / crashed`.
  Created only if `cfg.serve.supervise_daemon` (default true); otherwise a stub
  reporting state `external`. `stop()` SIGTERM → wait
  `daemon.shutdown_grace_seconds` → SIGKILL. Captures child stdout/stderr to a
  bounded ring buffer (last 200 lines).
- **Status fuses both planes:** PID / process-state from the supervisor +
  per-thread liveness from `daemon_heartbeats` (2B.2). Externally-supervised
  daemons still report full heartbeat status (read-only).
- **Unix socket** at `${runtime_dir}/localmail-supervisor.sock` (0600) so the
  CLI can talk to a running `serve`.
- HTTP (admin-gated, cookie-session, CSRF as per the auth design):
  ```
  GET  /v1/admin/daemon            # {state, pid, started_at, supervise_daemon_externally,
                                   #  heartbeats:[...], recent_log:[...]}
  POST /v1/admin/daemon/start      # Plane B
  POST /v1/admin/daemon/stop       # Plane B
  POST /v1/admin/daemon/restart    # Plane B
  POST /v1/admin/daemon/reload     # Plane A → enqueue reload-now
  POST /v1/admin/accounts/{id}/restart-sync   # Plane A → enqueue restart-account
  ```
- CLI: `localmail daemon {status,start,stop,restart,reload}` and
  `localmail daemon restart-account NAME`. `status` / `reload` /
  `restart-account` work against the DB planes even when the daemon is
  externally supervised; `start` / `stop` / `restart` require the socket and
  exit non-zero with the external-supervisor note when `supervise_daemon=false`.

### Tests (2B.4)

Supervisor start/stop/restart/crash against a dummy `sleep` subprocess (as the
approved design specifies); stub behaviour when `supervise_daemon=false`; route
tests (TestClient) for the five endpoints incl. CSRF + admin-gate; CLI parity.

---

## 2B.5 — Admin UI panel

`serve/admin/templates/daemon/panel.html` + a dashboard card:

- Status table: per-account idle/poll state, current folder, last heartbeat
  age (red past `heartbeat_stale_seconds`), last error.
- Buttons: start / stop / restart (disabled + note when externally
  supervised), **Reload now**, per-account **Restart sync**.
- HTMX `hx-get` partial polled `every 2s` while the page is open
  (`/admin/_partials/daemon-status`).
- All mutating controls carry the method-bound CSRF token
  (`(user_id, "<METHOD>:<action-url>")`, per #122/#125).

### Tests (2B.5)

Partial render with stale / error / external states; button gating;
CSRF-token presence and method binding.

---

## Cross-cutting decisions & risks

1. **Two planes, not one.** Lifecycle (start a *stopped* process) genuinely
   needs OS supervision and cannot be DB-mediated; everything else is
   DB-mediated so it survives the systemd deployment where `serve` does not
   supervise. Don't collapse them.
2. **Command queue ≠ account state.** Pause / resume / add / remove stay as
   `accounts` edits consumed by reconcile; the queue is only for actions with
   no desired-state representation. Resisting overlap keeps both simple.
3. **Single daemon instance** (multi-host is a non-goal): heartbeats + commands
   assume one consuming daemon. `FOR UPDATE SKIP LOCKED` is defensive, not a
   clustering claim. The startup heartbeat `DELETE` assumes the same.
4. **Migration numbering:** `0023_daemon_heartbeats.sql`, then
   `0024_daemon_commands.sql`. Re-check `ls migrations/` at each slice's
   plan-time in case another lands first.
5. **`config.toml` `[daemon].poll_seconds` per-account override** remains
   unhonoured (no DB column) — unchanged by this work.
6. **Teardown latency** is bounded by the IDLE heartbeat tick
   (`HEARTBEAT_SECONDS`), acceptable for reconcile; documented so nobody
   "fixes" it by shrinking `idle_renew_seconds`.

## Out of scope (future work, unchanged from the admin-UI design)

Auto-restart-on-crash, multi-host clustering, Windows server supervision,
admin audit log, per-thread CPU/mem metrics in heartbeats.
