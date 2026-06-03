# NEXT_SESSION.md — localmail handoff

> **Status as of 2026-06-03T0147 UTC.**
> Shipped **2B.5 — Admin daemon-control panel + 202-async lifecycle (#146)**,
> the **final 2B slice**. The sync daemon's lifecycle ops (start/stop/restart)
> are now non-blocking (run on a supervisor-owned thread; routes return 202; CLI
> polls to settle), and the admin UI gained a live daemon-control panel at
> `/admin/daemon`. Also lands the reusable **method-bound CSRF mint helper
> (#125)** the panel uses. Work is on branch
> **`daemon-control-2b5-panel-async`** (branched from `main` at `893b8b8`, the
> merged 2B.4), **pushed**, opened as **PR #147**
> (<https://github.com/hherb/localmail/pull/147>, **open, not yet merged**).
> Full suite **1217 passed**, mypy clean (84 files). **No new migration.**
>
> Last session's **PR #145 (2B.4 supervisor + HTTP + CLI) is MERGED** (`893b8b8`
> on main); its stale local + remote branch was deleted this session.
> **The 2B arc is now complete** (2B.1 hot-reload → 2B.5 panel). The remaining
> open admin-UI work is **Sub-plan 2A.3** (account CRUD screens), independent of
> 2B.

## Project context (1-minute version)

`localmail` mirrors IMAP accounts (Gmail OAuth, password) into Postgres,
**strictly read-only w.r.t. IMAP**. The **database is canonical for accounts**
end-to-end. The daemon hot-reloads its account set (2B.1), records per-thread
heartbeats (2B.2), consumes a DB command queue with LISTEN/NOTIFY wake (2B.3),
is supervised + controllable via two planes (2B.4), and now has **non-blocking
lifecycle control + an admin panel** (2B.5). Downstream consumers read the DB +
attachment tree directly or via the `localmail serve` HTTPS API. See
[CLAUDE.md](CLAUDE.md), [README.md](README.md), the 2B respec
[docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md](docs/superpowers/specs/2026-05-30-daemon-control-2b-respec-design.md),
and this slice's spec + plan
([spec](docs/superpowers/specs/2026-06-02-daemon-control-2b5-panel-async-lifecycle-design.md),
[plan](docs/superpowers/plans/2026-06-02-daemon-control-2b5-panel-async-lifecycle.md)).

## What we shipped this session

### Slice 2B.5 — admin panel + 202-async lifecycle

- **Supervisor async lifecycle (`daemon_supervisor.py`)** — `request_start()`,
  `request_stop()`, `request_restart()` set the **transitional** state
  (`starting`/`stopping`) synchronously under `_lock`, then run the existing
  blocking `start()`/`stop()`/`restart()` body on **one dedicated lifecycle
  thread**. A second lifecycle op while one is in flight raises
  `SupervisorUnavailable` (the **busy-guard**, keyed on
  `_lifecycle_thread.is_alive()` — not state, so a finished-then-dead thread
  passes through). Setting `stopping` *before* the SIGTERM keeps a clean async
  stop from being misread as `crashed` by the reader thread. The blocking
  variants stay (used by `close()` on serve shutdown — teardown must block — and
  by tests). `ExternalDaemonSupervisor` has matching `request_*` stubs that
  raise. New shared type alias `DaemonSupervisorT = DaemonSupervisor |
  ExternalDaemonSupervisor`.
- **Routes (`serve/admin/daemon_router.py`)** — `POST
  /v1/admin/daemon/{start,stop,restart}` call `request_*` and return **202** +
  transitional status; busy-guard / external stub → **409**. The GET-route
  fusion is extracted into `build_daemon_view(supervisor, conn, *,
  stale_seconds) -> dict` — the **single source** shared by the JSON route and
  the HTML panel.
- **Control socket (`serve/daemon_control_socket.py`)** — `handle_control_request`
  dispatches `start/stop/restart` to `request_*` (returns immediately with
  transitional status; no longer pins the per-connection handler thread for the
  grace period).
- **CLI (`daemon_cli.py`)** — `localmail daemon {start,stop,restart}` **poll
  `status` until settled** (`running`/`stopped`), `--no-wait` to skip. Named
  constants: `_LIFECYCLE_POLL_INTERVAL_S`, `_START_SETTLE_TIMEOUT_S` (reuses
  `_LIFECYCLE_TIMEOUT_BUFFER_S` + `_STATUS_TIMEOUT_S`). `op` typed
  `Literal["start","stop","restart"]`.
- **CSRF mint helper (`serve/admin/csrf.py`)** — `csrf_token_context(*,
  user_id, key)` returns `csrf_token_for` (legacy single-arg, for `base.html`'s
  body-wide htmx + logout tokens) and `csrf_token_for_method(method, action)`
  (method-bound, **the reusable #125 mint** for new admin HTML).
- **Admin panel (`serve/admin/daemon_panel_router.py` + `templates/daemon/`)** —
  `GET /admin/daemon` full page + self-polling HTMX partial
  `GET /admin/_partials/daemon-status` (the `#daemon-status` div re-carries its
  `hx-get`/`hx-trigger="every {{DAEMON_PANEL_POLL_SECONDS}}s"` after each
  `outerHTML` swap). Status table is red past `heartbeat_stale_seconds` (server
  `stale` flag, no client clock); lifecycle buttons **disabled when
  `supervise_daemon_externally`**; Plane-A reload + per-account restart-sync
  buttons stay enabled (**deduped per account** so idle+poll workers don't
  double-render). Each mutating control carries its own method-bound CSRF token.
  `/v1/admin/*` stays pure machine-JSON (no HTMX content negotiation).

### Commits on `daemon-control-2b5-panel-async` (oldest→newest)

```
b6573f7  docs: spec 2B.5 daemon panel + 202-async lifecycle (#146)
7bf92e2  docs: implementation plan for 2B.5 panel + async lifecycle
644ac62  feat(serve): async request_start/stop/restart on DaemonSupervisor (2B.5, #146)
62c7e58  feat(serve): daemon lifecycle routes return 202; busy-guard 409; build_daemon_view (2B.5, #146)
31b5f4a  refactor(serve): type build_daemon_view params; complete shape test (2B.5 review)
bd12090  feat(cli): daemon start/stop/restart poll until settled, add --no-wait (2B.5, #146)
b6ad520  refactor(cli): hoist test imports; type _lifecycle op as Literal (2B.5 review)
2c5913e  feat(serve): control socket dispatches lifecycle to request_* (2B.5, #146)
8eb8818  feat(serve): csrf_token_context mint helper (legacy + method-bound) (2B.5, #125)
176872e  feat(serve): admin daemon-control panel (2B.5)
bc5087f  fix(serve): dedupe restart-sync buttons; reuse signing-key helper; CSRF+XSS tests (2B.5 review)
c544e8b  docs: 2B.5 daemon panel + async lifecycle (#146, #125)
ec43cca  test(serve): widen busy-guard grace window for CI headroom (2B.5 review)
```

### Verification (this session)

- `unset VIRTUAL_ENV && uv run pytest -q tests/` — **1217 passed** (baseline
  1193 on merged main + 24 new: 6 supervisor, 3 routes, 2 socket, 3 CLI, 2 CSRF,
  8 panel — minus the 1 replaced `test_start_then_stop`, plus the shape-test
  extra).
- `unset VIRTUAL_ENV && uv run mypy src/localmail` — **clean, 84 files** (was 83).
- TDD throughout, executed subagent-driven: per-task spec + code-quality review,
  plus a final whole-branch review (assessment: **ready to merge**). All
  surfaced issues fixed (typed `build_daemon_view` params, restart-sync dedupe +
  CSRF test, autoescape/XSS test, CI grace-window headroom).

## What's next

### 0. **Review & merge PR #147** *(immediate)*

PR #147 (<https://github.com/hherb/localmail/pull/147>) is **open and green**
(1217 passed, mypy clean). It fully resolves **#146** (close on merge) and
advances **#125** (the method-bound mint helper now exists + is used by the
panel; #125's actual subject — the *account* HTML screens — is 2A.3, so leave
#125 open until 2A.3 adopts the helper). After merge:

```bash
gh pr merge 147 --squash --delete-branch
git checkout main && git fetch --prune origin && git merge --ff-only origin/main
git branch -D daemon-control-2b5-panel-async
gh issue close 146   # fully resolved by #147
```

### 1. **Sub-plan 2A.3 — account CRUD admin screens** *(next real feature)*

The 2B arc is done; the remaining open admin-UI work is account management
screens. Service layer already exists
([src/localmail/api/admin/accounts.py](src/localmail/api/admin/accounts.py):
`list_accounts`, `get_account`, `create_account`, `update_account`,
`delete_account`, `store_password`, `clear_secret`, `probe_connection`) and the
web OAuth flow ([api/admin/oauth.py](src/localmail/api/admin/oauth.py)). 2A.3 is
the HTML UI on top.
- **Reuse, don't reinvent:** mint method-bound CSRF via the new
  `csrf_token_context().csrf_token_for_method` (closes #125's intent); follow
  the panel's HTMX self-poll / per-button `hx-headers` pattern; the nav already
  links `/admin/accounts`.
- **Acceptance:** list/create/edit/delete account screens; password + OAuth flows
  wired to the existing service layer + JSON routes; per-control method-bound
  CSRF; TDD; no magic numbers. There is no spec/plan yet — **brainstorm → spec →
  plan first** (the routes exist but the screen design doesn't).

### 2. **Other open arcs / deferred** *(unchanged)*

- Externally-blocked / measured: **#90** (glib/Tauri Dependabot — the moderate
  alert GitHub flags on push), **#47** (extract_worker transient opt-in),
  **#25** (websockets.legacy depwarn), **#5** (search batch INSERT), **#134**
  (oauth_state flake — environmental).
- **Open issues after #147 merges + #146 closes: 5** (#5, #25, #47, #90, #125).

## Open decisions & risks

1. **Two planes, not one** *(carried)*. Lifecycle (start a *stopped* process)
   needs OS supervision over the socket (Plane B); reload/restart-sync are
   DB-mediated (Plane A) and survive the systemd deploy. The panel's
   start/stop/restart buttons are Plane B; reload/restart-sync are Plane A.
2. **`request_*` busy-guard keys on `_lifecycle_thread.is_alive()`, not state.**
   A finished (dead) thread passes through; only an *in-flight* op blocks a new
   one. Don't "fix" the lingering dead-thread reference into a state check — the
   liveness check is the correct authority and `_spawn_lifecycle` overwrites the
   ref on the next op.
3. **`close()` (serve shutdown) calls the BLOCKING `stop()`** — teardown must
   wait for the child to die. Only the operator-facing routes/socket/CLI go
   async. **Narrow follow-up risk** (from final review): if a `request_restart`
   worker is exactly between its `stop()` and `start()` halves when
   `supervisor.close()` runs, the restart's `start()` can re-spawn a child after
   close. Mitigated by systemd `KillMode=control-group`; a bare `kill <serve>`
   could orphan it. Not worth a "closing flag" today — note if it ever bites.
4. **202, not 200, for lifecycle routes** — the contract is "accepted, poll to
   settle". The panel polls `GET /v1/admin/daemon` every
   `DAEMON_PANEL_POLL_SECONDS` (2s); the CLI polls the socket. Don't revert to
   synchronous 200 (that reintroduces #146).
5. **Panel buttons use `hx-swap="none"`**, so a busy-guard **409** or CSRF
   **400** is currently only reflected on the next poll tick (no toast). This is
   the documented optional follow-up from the spec (endpoint-pure
   `hx-on::after-request` event refresh) — add it if operators find the silent
   rejection confusing. `/v1/admin/*` must stay pure machine-JSON; don't
   content-negotiate HTML into them.
6. **`build_daemon_view` is the only fusion** — both the JSON route and the HTML
   partial go through it. Keep it that way; don't inline a second copy.
7. **Method-bound CSRF mint is `csrf_token_context` in
   [serve/admin/csrf.py](src/localmail/serve/admin/csrf.py)** — the canonical
   #125 helper. 2A.3 must use `csrf_token_for_method`, not the legacy single-arg.
8. **Migration numbering** — latest applied is **0024** (daemon_commands). 2B.5
   added **no** migration. Next free slot: `0025_*.sql`. Re-check
   `ls migrations/` at plan-time.
9. **No ROADMAP.md** in this repo *(carried)* — slice status lives in
   NEXT_SESSION/handoffs + the specs.
10. **Heartbeat vocabulary still load-bearing** *(carried)* — any new heartbeat
    call site must use a `worker_kind`/`state` present in both the SQL CHECK
    lists (0023) and the `WorkerKind`/`WorkerState` Literals; all loop
    heartbeats go through `safe_heartbeat`.
11. **Tooling note** *(carried)* — the full-suite run emits harmless psycopg
    pool `__del__` ResourceWarnings at interpreter teardown — *not* a failure
    (`1217 passed`).
12. **`.claude/` local files** stay untracked, by design.

## Exact commands to resume

```bash
cd /Users/hherb/src/localmail
git fetch --prune origin                 # ALWAYS first

git status                               # clean apart from .claude/ local files
git branch -vv                           # main + daemon-control-2b5-panel-async (pushed, PR #147)
git --no-pager log --oneline -12
gh issue list --state open --limit 40    # 6 open now; 5 after #146 closes on merge

# Verify state (expect 1217 passed, mypy clean):
unset VIRTUAL_ENV && uv run pytest -q tests/
unset VIRTUAL_ENV && uv run mypy src/localmail
# This slice's tests specifically:
unset VIRTUAL_ENV && uv run pytest -q tests/test_daemon_supervisor.py \
    tests/test_serve_daemon_routes.py tests/test_daemon_control_socket.py \
    tests/test_daemon_cli.py tests/test_admin_csrf.py tests/test_serve_daemon_panel.py
```

Pick up **2A.3 (account CRUD admin screens)** after PR #147 merges:

```bash
git checkout main && git pull
git checkout -b admin-ui-2a3-account-screens
ls migrations/    # no new migration expected; latest is 0024
# brainstorm → spec → plan first (routes exist; screen design does not)
```

## File map (this session)

```
NEXT_SESSION.md                                          # REPLACED this session
docs/superpowers/specs/2026-06-02-daemon-control-2b5-panel-async-lifecycle-design.md   # NEW spec
docs/superpowers/plans/2026-06-02-daemon-control-2b5-panel-async-lifecycle.md          # NEW plan
src/localmail/serve/daemon_supervisor.py                 # +request_start/stop/restart + busy-guard + DaemonSupervisorT; stub request_*
src/localmail/serve/admin/daemon_router.py               # routes → 202; busy-guard 409; build_daemon_view extracted+typed
src/localmail/serve/daemon_control_socket.py             # dispatch start/stop/restart → request_*; Protocol grows request_*
src/localmail/daemon_cli.py                              # poll-until-settled + --no-wait; named constants; Literal op
src/localmail/serve/admin/csrf.py                        # +csrf_token_context (legacy + method-bound mint, #125)
src/localmail/serve/admin/daemon_panel_router.py         # NEW — /admin/daemon page + /admin/_partials/daemon-status partial
src/localmail/serve/admin/templates/daemon/panel.html    # NEW — full page (extends base)
src/localmail/serve/admin/templates/daemon/_status.html  # NEW — self-polling status fragment + buttons
src/localmail/serve/admin/static/admin.css               # +.daemon-* styles
src/localmail/serve/app.py                               # mount admin_daemon_panel_router at /admin
README.md                                                # daemon-control: poll/--no-wait + panel section
CLAUDE.md                                                # +2B.5 bullet; migrations note → 2B.4/2B.5 no migration
tests/test_daemon_supervisor.py                          # +6 async lifecycle tests
tests/test_serve_daemon_routes.py                        # 202/409/build_daemon_view; grace-window headroom
tests/test_daemon_control_socket.py                      # +async dispatch tests
tests/test_daemon_cli.py                                 # +poll-until-settled/--no-wait/crashed tests; hoisted imports
tests/test_admin_csrf.py                                 # +csrf_token_context tests
tests/test_serve_daemon_panel.py                         # NEW — 8 panel tests (render/stale/error/external/CSRF/XSS)
docs/handoffs/2026-06-03T0147-utc-post-2b5-daemon-panel.md   # frozen snapshot of this file
```

`main` at `893b8b8` (== `origin/main`, the merged #145). Branch
`daemon-control-2b5-panel-async` **pushed** (== its `origin/` ref), **PR #147
open**. Working tree clean (only `.claude/` local files). 2 local branches
(`main`, `daemon-control-2b5-panel-async`); 1 open PR (#147).
