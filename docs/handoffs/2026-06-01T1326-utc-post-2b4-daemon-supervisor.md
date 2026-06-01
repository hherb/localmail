# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-01T1326 UTC.**
> Shipped **2B.4 — DaemonSupervisor + HTTP + CLI** (the OS-facing half of daemon
> control), layered on the DB planes from 2B.1–2B.3. Two control planes:
> **Plane A** (DB-mediated, always available) — `reload` + per-account
> `restart-sync` enqueue into the 2B.3 `daemon_commands` queue; **Plane B**
> (process lifecycle) — `localmail serve` optionally owns `localmail run` as a
> child subprocess, controlled in-process and over a Unix control socket. Work
> is on branch **`daemon-control-2b4-supervisor`** (branched from `main` at
> `4abc587`, the merged 2B.3), **pushed**, opened as **PR #145**
> (<https://github.com/hherb/localmail/pull/145>, **open, not yet merged**).
> Full suite **1193 passed**, mypy clean (83 files). **No new migration.**
>
> Last session's **PR #144 (2B.3 command queue) is MERGED** (`4abc587` on main);
> its stale local + remote branch was deleted this session.
> The remaining 2B arc is **2B.5** (admin UI panel) — the last slice.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. The daemon hot-reloads its account set (2B.1), records per-thread
heartbeats (2B.2), consumes a DB command queue with LISTEN/NOTIFY wake (2B.3),
and is now **supervised + controllable** via two planes (2B.4). Downstream
consumers read the DB + attachment tree directly or via the `localmail serve`
HTTPS API. See [CLAUDE.md](CLAUDE.md), [README.md](README.md), and the 2B spec
[docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md)
(§2B.5 is what's next).

## What we shipped this session

### Slice 2B.4 — DaemonSupervisor + HTTP + CLI

- **Config (`ServeConfig`)** — `supervise_daemon: bool = True` and
  `runtime_dir: str = ""` (empty = resolve from `$XDG_RUNTIME_DIR`, falling back
  to the platform temp dir). Documented in `config.example.toml`. No magic numbers.
- **`serve/daemon_supervisor.py`** — Plane B. `DaemonSupervisor` owns
  `localmail run` via `subprocess.Popen`: state machine
  `stopped → starting → running → stopping → stopped`, with `crashed` for an
  unexpected child exit (the stdout reader thread hits EOF while state is still
  `running`); a bounded ring buffer of combined child stdout/stderr (a **fresh
  `deque` is bound per `start()`** and handed to that run's reader, so a
  late-draining crashed-run reader can't leak stale lines into the next run);
  `stop()` = SIGTERM → wait `daemon.shutdown_grace_seconds` → SIGKILL, and
  **releases the lock before waiting** so the reader can never deadlock against
  the grace wait. `ExternalDaemonSupervisor` is the stub for
  `supervise_daemon=false` (status `external`; lifecycle ops raise
  `SupervisorUnavailable`). Pure helpers `resolve_runtime_dir`, `socket_path`,
  `default_daemon_argv`, `status_to_dict` are shared by serve + CLI.
  **`src/localmail/__main__.py`** makes `python -m localmail run` work (portable
  launch, no PATH dependence).
- **`serve/daemon_control_socket.py`** — newline-delimited JSON over a Unix
  socket at `${runtime_dir}/localmail-supervisor.sock` (mode 0600).
  `handle_control_request` is a pure dispatcher (never raises);
  `ControlSocketServer` wraps it; `send_control_request` is the CLI client half.
  **Each connection is handled on its own daemon thread with a bounded
  recv/send timeout** so a slow/silent client can't wedge the accept loop or
  freeze the control plane while a 30 s `stop()` runs (review fix — see below).
- **`serve/admin/daemon_router.py`** — admin-gated, method-bound-CSRF routes
  under `/v1/admin`: `GET /daemon` (fuses supervisor process state +
  `daemon_heartbeats` + recent log; `supervise_daemon_externally` derives from
  the supervisor's own `state == external`, not config); `POST
  /daemon/{start,stop,restart}` (Plane B; **409** on the external stub); `POST
  /daemon/reload` and `POST /accounts/{id}/restart-sync` (Plane A → 2B.3
  `enqueue_command`, **reused not re-implemented**; restart-sync **404s an
  unknown account before enqueue**).
- **`create_app` wiring** — supervisor on `app.state.daemon_supervisor` (real
  when `supervise_daemon`, stub otherwise), **side-effect-free at construction**
  (child spawns only on `start()`; the control socket binds only in the lifespan
  when `enable_control_socket=True`, the `serve` path) so TestClient apps never
  bind a shared socket. `serve_cmd` threads `daemon_config` +
  `daemon_config_path` + `enable_control_socket=serve_cfg.supervise_daemon`.
- **`daemon_cli.py`** (registered via `main.add_command(daemon_group)`) —
  `localmail daemon {status,reload,restart-account}` work against the DB planes
  even when externally supervised; `{start,stop,restart}` go over the socket and
  exit non-zero with a clear note when `supervise_daemon=false` (external) or the
  socket is unreachable (serve not running). `status` always prints heartbeats.

### Commits on `daemon-control-2b4-supervisor` (oldest→newest)

```
66ebd37  feat(serve): supervise_daemon + runtime_dir config (2B.4)
c21b43c  feat(serve): DaemonSupervisor process lifecycle + ring buffer (2B.4)
137b859  feat(serve): Unix control socket for the daemon supervisor (2B.4)
de29231  feat(serve): wire supervisor + admin daemon control routes (2B.4)
88c90c7  feat(cli): localmail daemon subgroup (2B.4)
0458426  docs: record 2B.4 daemon supervisor + control + CLI
```

(The two review fixes — per-connection socket threads/timeout + fresh-deque-
per-run — were folded into commits `137b859` and `c21b43c` before commit, plus a
silent-client regression test in `tests/test_daemon_control_socket.py`.)

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1193 passed** (baseline
  1137 on merged main + 56 new: 4 config, 19 supervisor, 13 socket, 4 wiring,
  9 routes, 7 CLI).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 83 files** (was 78).
- TDD throughout. A full-branch code review found two material issues — the
  single-threaded accept loop with no per-connection timeout (a silent client
  could freeze the whole control plane) and stale-log contamination across runs
  — both fixed and the socket-robustness one pinned by a regression test.

## What's next

### 0. **Review & merge PR #145** *(immediate)*

PR #145 (<https://github.com/hherb/localmail/pull/145>) is **open and green**
(1193 passed, mypy clean). After merge:

```bash
gh pr merge 145 --squash --delete-branch
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D daemon-control-2b4-supervisor
```

### 1. **2B.5 — Admin UI panel** *(the last 2B slice)*

Per [the spec](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md) §2B.5:
- `serve/admin/templates/daemon/panel.html` + a dashboard card.
- Status table: per-account idle/poll state, current folder, last heartbeat age
  (**red past `heartbeat_stale_seconds`**), last error — sourced from the
  `GET /v1/admin/daemon` shape this session shipped.
- Buttons: start / stop / restart (**disabled + note when externally
  supervised** — gate on the `supervise_daemon_externally` field), **Reload
  now**, per-account **Restart sync**.
- HTMX `hx-get` partial polled `every 2s` while the page is open
  (`/admin/_partials/daemon-status`).
- All mutating controls carry the **method-bound CSRF token**
  (`(user_id, "<METHOD>:<action-url>")`, per #122/#125) — fold in #125 here
  since this is the first new admin HTML UI to mint method-bound tokens.
- **Wire to this session's endpoints**, don't re-implement: the buttons POST to
  `/v1/admin/daemon/{start,stop,restart,reload}` and
  `/v1/admin/accounts/{id}/restart-sync`; the partial GETs `/v1/admin/daemon`.
- **Acceptance:** partial renders with stale / error / external states; button
  gating when externally supervised; CSRF-token presence + method binding. All
  TDD; no magic numbers.

That closes the 2B arc.

### 2. **Other open arcs / deferred** *(unchanged)*

- **Admin-UI Sub-plan 2A.3** (account screens; fold #125 method-bound CSRF mint)
  — independent of 2B, still open. 2B.5 and 2A.3 both touch the admin UI and
  both need #125; consider doing #125's mint helper once and sharing.
- Externally-blocked / measured: **#90** (glib/Tauri Dependabot — the moderate
  alert GitHub flags on push), **#47** (extract_worker transient opt-in),
  **#25** (websockets.legacy depwarn), **#5** (search batch INSERT), **#134**
  (oauth_state flake — environmental).
- **Open issues: 6** (#5, #25, #47, #90, #125, #134). 2B.4 had no tracking issue.

## Open decisions & risks

1. **Two planes, not one.** Lifecycle (start a *stopped* process) needs OS
   supervision and can't be DB-mediated; everything else is DB-mediated so it
   survives the systemd deploy where `serve` does not supervise. Don't collapse
   them in 2B.5's UI — the start/stop/restart buttons are Plane B (socket),
   reload/restart-sync are Plane A (queue).
2. **`supervise_daemon_externally` derives from the supervisor's reported
   `state == external`, NOT from `serve_config.supervise_daemon`.** This is
   deliberate — it lets tests swap a stub onto `app.state` and have the GET
   report correctly. In production the two always agree. 2B.5 gates its buttons
   on this wire field.
3. **The control socket is bound only by the lifespan when
   `enable_control_socket=True`** (the `serve` CLI path), never at `create_app`
   construction. Don't move socket binding into `create_app` — every TestClient
   app would then bind a shared socket and collide.
4. **The supervised child is `python -m localmail run`** with the serve
   process's `--config` threaded in (`default_daemon_argv`). If `serve` is
   launched with `LOCALMAIL_DSN_OVERRIDE` (no config file), `daemon_config_path`
   is the resolved default path; the child re-reads config the same way `serve`
   would. Keep this in mind if 2B.5 ever surfaces "what config is the daemon
   using".
5. **`stop()` holds no lock during the grace wait** and the reader thread only
   grabs the lock at EOF — verified deadlock-free in review. Don't reintroduce a
   lock-across-`proc.wait()`.
6. **Per-connection socket timeout = `DEFAULT_CONN_TIMEOUT_S` (10 s)** bounds a
   stuck client's handler thread; it does NOT bound the lifecycle op itself
   (a `stop()` up to `shutdown_grace_seconds` is not socket-bound). The CLI's
   own read timeout for lifecycle ops is `shutdown_grace_seconds +
   _LIFECYCLE_TIMEOUT_BUFFER_S`. Keep these consistent if grace changes.
7. **Migration numbering.** Latest applied is **0024** (daemon_commands). 2B.4
   added **no** migration. Next free slot: `0025_*.sql`. 2B.5 (templates + a
   partial route) needs **no** migration. Re-check `ls migrations/` at plan-time.
8. **No ROADMAP.md exists** in this repo. Slice status lives in
   NEXT_SESSION/handoffs + the spec.
9. **Heartbeat vocabulary still load-bearing** (carried): any new heartbeat call
   site must use a `worker_kind`/`state` present in both the SQL CHECK lists
   (migration 0023) and the `WorkerKind`/`WorkerState` Literals; all loop
   heartbeats go through `safe_heartbeat`.
10. **Tooling note** (carried): the full-suite run emits a harmless psycopg pool
    `__del__` ResourceWarning at interpreter teardown — *not* a failure
    (`1193 passed`).
11. **`.claude/` local files** stay untracked, by design.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/ local files
git branch -vv                           # main + daemon-control-2b4-supervisor (pushed, PR #145)
git --no-pager log --oneline -10
gh issue list --state open --limit 40    # 6 open

# Verify state (expect 1193 passed, mypy clean):
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
# This slice's tests specifically:
unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_supervisor.py \
    tests/test_daemon_control_socket.py tests/test_serve_daemon_routes.py \
    tests/test_serve_daemon_wiring.py tests/test_daemon_cli.py
unset VIRTUAL_ENV && uv run pytest -q tests/test_config_serve_admin.py
```

Pick up **2B.5 (admin UI panel)** after PR #145 merges:

```bash
git checkout main && git pull
git checkout -b daemon-control-2b5-admin-ui
# Plan from docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md §2B.5
ls migrations/    # no new migration needed; latest is 0024
```

## File map (this session)

```
NEXT_SESSION.md                                 # REPLACED this session
src/localmail/__main__.py                       # NEW — `python -m localmail` shim (portable child launch)
src/localmail/config.py                         # +ServeConfig.supervise_daemon (True) +runtime_dir ("")
src/localmail/serve/daemon_supervisor.py        # NEW — DaemonSupervisor + ExternalDaemonSupervisor + pure helpers
src/localmail/serve/daemon_control_socket.py    # NEW — Unix control socket (newline-JSON) + client
src/localmail/serve/admin/daemon_router.py      # NEW — admin daemon control routes (Plane A + B)
src/localmail/serve/app.py                      # supervisor on app.state; lifespan socket bind; +daemon_config/daemon_config_path/enable_control_socket
src/localmail/cli.py                            # +daemon_group registration; serve_cmd threads daemon config + enables socket
src/localmail/daemon_cli.py                     # NEW — `localmail daemon {status,reload,restart-account,start,stop,restart}`
config.example.toml                             # [serve] supervise_daemon + runtime_dir knobs
README.md                                       # daemon-control CLI section; run-row enqueue clause
CLAUDE.md                                        # 2B.4 bullet; migrations line → 0024
tests/test_config_serve_admin.py                # +4 supervise_daemon/runtime_dir tests
tests/test_daemon_supervisor.py                 # NEW — 19 supervisor tests (lifecycle, crash, ring buffer, stub)
tests/test_daemon_control_socket.py             # NEW — 13 socket tests (dispatch, round trip, 0600, silent-client)
tests/test_serve_daemon_wiring.py               # NEW — 4 create_app wiring tests
tests/test_serve_daemon_routes.py               # NEW — 9 route tests (auth, CSRF, lifecycle, enqueue)
tests/test_daemon_cli.py                        # NEW — 7 CLI tests (plane gating)
docs/handoffs/2026-06-01T1326-utc-post-2b4-daemon-supervisor.md  # frozen snapshot of this file
```

`main` at `4abc587` (== `origin/main`, the merged #144). Branch
`daemon-control-2b4-supervisor` **pushed** (== its `origin/` ref), **PR #145
open**. Working tree clean (only `.claude/` local files). 2 local branches
(`main`, `daemon-control-2b4-supervisor`); 1 open PR (#145).
